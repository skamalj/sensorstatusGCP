import argparse
import logging
from apache_beam import DoFn, io, ParDo, Pipeline
import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions, GoogleCloudOptions


# Read DB connect parameters from GCP Secret Manager
def read_secret_params(project_id, secret_id):
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    secret_name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    return client.access_secret_version(request={"name": secret_name}).payload.data.decode("UTF-8")

# Create a KV pair for each event. This is needed for stateful DoFns as state works on each key independently.
def create_kv_events(element):
    import json
    json_element = json.loads(element.decode('utf-8'))
    return (json_element['source']['device_id'], json.dumps(json_element))


# This DoFn sets the timer for the devices. DB entry for 'ACTIVE' device is created only on first event arrival.
# Subsequent events only update or move the timer ahead. This is done to avoid multiple DB writes for the same device.
# Do note, this DoFn does not actually write records to Yugabyte, but only yiels the record,
# This output is used in subsequent DoFns for logging and writing to Yugabyte.
class ConfigureTimerDoFn(DoFn):
    from apache_beam.transforms.userstate import TimerSpec, ReadModifyWriteStateSpec, on_timer, TimeDomain
    from apache_beam.coders.coders import VarIntCoder, StrUtf8Coder
    from apache_beam import DoFn

    EVENTTIME_STATE_SPEC = ReadModifyWriteStateSpec(
        'last_event_time', VarIntCoder())
    EVENTDATA_STATE_SPEC = ReadModifyWriteStateSpec(
        'last_event_data', StrUtf8Coder())
    TIMER = TimerSpec('timer', TimeDomain.REAL_TIME)

    def __init__(self, timer_timeout):
        self.timer_timeout = timer_timeout

    def process(self,
                element_pair,
                eventtime_state=DoFn.StateParam(EVENTTIME_STATE_SPEC),
                eventdata_state=DoFn.StateParam(EVENTDATA_STATE_SPEC),
                timer=DoFn.TimerParam(TIMER)):

        from apache_beam.utils.timestamp import Timestamp
        import json
        # Set a timer to go off 15 seconds in the future.
        new_time = Timestamp(micros=json.loads(element_pair[1])[
                             'eventtime']*1000 + self.timer_timeout*1000*1000)
        current_value = eventtime_state.read() or 0
        if new_time > current_value:
            eventtime_state.write(new_time)
            eventdata_state.write(element_pair[1])
            timer.set(new_time)
            if current_value == 0:
                json_element = json.loads(element_pair[1])
                yield (element_pair[0], json_element['eventtime'], Timestamp.now(),
                       json_element['source']['sensor_type'], json_element['source']['factory_id'],
                       json_element['source']['section'], 'ACTIVE')

    @on_timer(TIMER)
    def expiry_callback(self, eventdata_state=DoFn.StateParam(EVENTDATA_STATE_SPEC),
                        eventtime_state=DoFn.StateParam(EVENTTIME_STATE_SPEC)):
        import json
        from apache_beam.utils.timestamp import Timestamp
        if not eventdata_state.read() is None:
            json_element = json.loads(eventdata_state.read())
            eventdata_state.clear()
            eventtime_state.clear()
            yield (json_element['source']['device_id'], json_element['eventtime'], Timestamp.now(),
                   json_element['source']['sensor_type'], json_element['source']['factory_id'],
                   json_element['source']['section'], 'FAILED')

# YSQL can utilize postgresql driver to connect to YugabyteDB. psycopsg2 library does not work with Dataflow.
# Reason being this needs native 'C' libraries, which are not avaialble in Dataflow. Only way to get around this is to
# create a custom image. 
# Hence pg8000 library is used.

# There is also an issue with how SSLContext is configured for both YSQL and YCQL.  This needs CA certificate file instead of string. 
# Since we are working with distributed technology, we need to ensure that certificate file is created on each 
# worker node. So this is created in 'setup' function, which is executed once or more times on each node (but not for each record)
class writeToYSQL(DoFn):
    def __init__(self, table, database, project_id, port, user_secret_name,
                 password_secret_name, host_secret_name, ca_cert_secret_name):
        self.table = table
        self.database = database
        self.tmp_ca_cert = '/tmp/ybca.crt'
        self.sql_port = port
        self.sql_user = read_secret_params(
            project_id, user_secret_name)
        self.sql_pass = read_secret_params(
            project_id, password_secret_name)
        self.sql_host = read_secret_params(
            project_id, host_secret_name)
        self.sql_ca_cert = read_secret_params(
            project_id, ca_cert_secret_name)

    # Function needed to create CA cert file in tmp folder for databases
    # CA CRT value is save in Secret Mamnager, the data is retrieved as string and saved as file
    def create_file(self, file_path, data):
        from genericpath import exists

        if not exists(file_path):
            with open(file_path, "w") as file:
                file.write(data)

    def setup(self):
        from ssl import SSLContext, PROTOCOL_TLSv1_2, CERT_REQUIRED
        import pg8000.dbapi
        import logging
        try:
            self.create_file(self.tmp_ca_cert, self.sql_ca_cert)
            ssl_context = SSLContext(PROTOCOL_TLSv1_2)
            logging.info("YSQL Setup in progress: CA cert file created")
            ssl_context.load_verify_locations(self.tmp_ca_cert)
            ssl_context.verify_mode = CERT_REQUIRED
            self.connection = pg8000.dbapi.connect(
                user=self.sql_user,
                password=self.sql_pass,
                database=self.database,
                host=self.sql_host,
                port=self.sql_port,
                ssl_context=ssl_context
            )
            self.cursor = self.connection.cursor()
            logging.info("YSQL Setup complete: Connection created")
        except Exception as e:
            logging.info("YSQL Setup failed: " + str(e))
            raise e

    def teardown(self):
        self.connection.close()

    # Commit batch in finish bundle, this is not needed for TCQL
    def finish_bundle(self):
        self.connection.commit()

    # Device is primary key, whose status is updated/created in this function. Hence 'INSERT...ON CONFLICT' is used.
    def process(self, element):
        insert_stmt = f"""INSERT INTO {self.table} (device_id,eventtime,updated_at,sensor_type,factory_id,section,status) 
                values (%s,to_timestamp(%s/1000.),%s,%s,%s,%s,%s)
                ON CONFLICT (device_id) DO UPDATE SET eventtime=to_timestamp(%s/1000.),updated_at=%s,status=%s"""
        try:
            record = (element[0], element[1], element[2].to_utc_datetime(
            ), element[3], element[4], element[5], element[6], element[1], element[2].to_utc_datetime(
            ), element[6])
            self.cursor.execute(insert_stmt, record)
        except Exception as e:
            logging.info(
                "YSQL: Insertion failed for record: {record} with error: {e}")
            raise e


class writeToYCQL(DoFn):
    def __init__(self, table, project_id, keyspace, cql_port, cql_user_secret_name,
                 cql_password_secret_name, cql_host_secret_name, cql_ca_cert_secret_name):
        self.table = table
        self.keyspace = keyspace
        self.tmp_ca_cert = '/tmp/ybca.crt'
        self.cql_port = cql_port
        self.cql_user = read_secret_params(
            project_id, cql_user_secret_name)
        self.cql_pass = read_secret_params(
            project_id, cql_password_secret_name)
        self.cql_host = read_secret_params(
            project_id, cql_host_secret_name)
        self.cql_ca_cert = read_secret_params(
            project_id, cql_ca_cert_secret_name)

    # Function needed to create CA cert file in tmp folder for databases
    # CA CRT value is save in Secret Mamnager, the data is retrieved as string and saved as file
    def create_file(self, file_path, data):
        from genericpath import exists

        if not exists(file_path):
            with open(file_path, "w") as file:
                file.write(data)

    def setup(self):
        from ssl import SSLContext, PROTOCOL_TLSv1_2, CERT_REQUIRED
        from cassandra.cluster import Cluster
        from cassandra.policies import RoundRobinPolicy
        from cassandra.auth import PlainTextAuthProvider
        import logging
        try:
            ssl_context = SSLContext(PROTOCOL_TLSv1_2)
            self.create_file(self.tmp_ca_cert, self.cql_ca_cert)
            logging.info("YCQL Setup in progress: CA cert file created")
            ssl_context.load_verify_locations(self.tmp_ca_cert)
            ssl_context.verify_mode = CERT_REQUIRED
            auth_provider = PlainTextAuthProvider(
                username=self.cql_user, password=self.cql_pass)
            cluster = Cluster([self.cql_host], protocol_version=4,
                              load_balancing_policy=RoundRobinPolicy(),
                              ssl_context=ssl_context, auth_provider=auth_provider,
                              port=self.cql_port, connect_timeout=10)
            self.session = cluster.connect()
            logging.info("YCQL Setup complete: Session created")
        except Exception as e:
            logging.info("YSQL Setup failed: " + str(e))
            raise e

    def teardown(self):
        self.session.shutdown()

    def process(self, element):
        import json
        json_element = json.loads(element.decode('utf-8'))
        insert_stmt = f"INSERT INTO {self.keyspace}.{self.table} (device_id,eventtime,sensor_type,factory_id,section,value) values (%s,%s,%s,%s,%s,%s)"
        try:
            self.session.execute(insert_stmt, (json_element['source']['device_id'], json_element['eventtime'],
                                               json_element['source']['sensor_type'], json_element['source']['factory_id'],
                                               json_element['source']['section'], json_element['value']))
        except Exception as e:
            logging.info("YCQL Insertion failed: " + str(e))
            raise e


def run(input_sub, timer_timeout, pipeline_args=None):
    # Set `save_main_session` to True so DoFns can access globally imported modules.
    pipeline_options = PipelineOptions(
        pipeline_args, streaming=True, save_main_session=True
    )

    with Pipeline(options=pipeline_options) as pipeline:
        project_id = pipeline_options.view_as(GoogleCloudOptions).project
        events = pipeline | "Read from Pub/Sub" >> io.ReadFromPubSub(
            subscription=f"projects/{project_id}/subscriptions/{input_sub}")
        events | "Write Raw Data to YB" >> ParDo(writeToYCQL(
            table="raw_data",
            project_id=project_id,
            keyspace="pubsub",
            cql_port=9042,
            cql_user_secret_name="cql_user",
            cql_password_secret_name="cql_password",
            cql_host_secret_name="cql_host",
            cql_ca_cert_secret_name="cql_ca_cert"
        ))
        kv_events = events | "Create KV for StateTime" >> beam.Map(
            create_kv_events)
        device_status = kv_events | "Configure Event Timer" >> ParDo(
            ConfigureTimerDoFn(timer_timeout))
        device_status | "Log Device Status" >> beam.Map(logging.info)
        device_status | "Write Device Status to YB" >> ParDo(writeToYSQL(
            table="status",
            database="yugabyte",
            project_id=project_id,
            port=5433,
            user_secret_name="cql_user",
            password_secret_name="cql_password",
            host_secret_name="cql_host",
            ca_cert_secret_name="cql_ca_cert"
        ))


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input_sub",
        help="The Cloud Pub/Sub subscription to read from.", default="sourcesub"
    )
    parser.add_argument(
        "--timer_timeout",
        help="Timeout in seconds. Device will be marked as Failed when no data is recieved for this duration", default=30
    )
    known_args, pipeline_args = parser.parse_known_args()

    run(
        known_args.input_sub,
        known_args.timer_timeout,
        pipeline_args,
    )
