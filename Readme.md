This repo builds following architecture on GCP for identifying failed industrial sensors in real time

![Architecture](resources\SensorGCP.png)

## Deploy

* Create YCQL and YSQL tables in Yugabyte cloud instance. Create table script are in `resources/createyb.txt` 

* Create DB Secrets in GCP secret Manager. Since credentials are same for YCQL and YSQL, code use same for both connections

```
gcloud secrets create cql_user --replication-policy="automatic"
gcloud secrets create cql_password --replication-policy="automatic"
gcloud secrets create cql_host --replication-policy="automatic"
gcloud secrets create cql_ca_cert --replication-policy="automatic"

echo -n "<YB Username>" | gcloud secrets versions add cql_user --data-file=-
echo -n "<YB Password>" | gcloud secrets versions add cql_password --data-file=-
echo -n "<YB Host>" | gcloud secrets versions add cql_host --data-file=-
echo -n $(cat /home/kamal/root.crt) | gcloud secrets versions add cql_ca_cert --data-file=-

```

* Create a pubsub topic (any name will do) and subscription name `sourcesub`. If you decide to change the subscription name 
then provide that as commandline parameter
```
--input_sub <subscription name>
```

* Submit the job to dataflow (Add input_sub param if required)
```
python process.py \
    --region europe-west3 \
    --runner DataflowRunner \
    --project <your project id> \
    --temp_location gs://beam-storage-eu-west/tmp/ \
    --save_main_session True \
    --requirements_file requirements.txt
```

* Dataflow job takes about 10-15mins to actually startup with default compute nodes.

* Now start your [generator](https://github.com/skamalj/datagenerator) based on cconfig file provided in resources folder.
Do remeber to set you project and topic name in .env file.

## Note
>> psycopg2 postgresql library does not work straight away with dataflow, as it need OS level packages to be installed as well as a dependency. This will need custom ima0ge to work with, hence pg8000 was used which is native python. 