import time
import boto3

bedrock_agent = boto3.client("bedrock-agent")


def handler(event, context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    kb_id = props["KnowledgeBaseId"]
    ds_id = props["DataSourceId"]

    if request_type in ("Create", "Update"):
        resp = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
        )
        job_id = resp["ingestionJob"]["ingestionJobId"]

        # Poll until the ingestion job finishes (or the 10-minute Lambda
        # timeout forces CloudFormation to retry / report failure).
        deadline = time.time() + 540
        status = "STARTING"
        while time.time() < deadline:
            job = bedrock_agent.get_ingestion_job(
                knowledgeBaseId=kb_id,
                dataSourceId=ds_id,
                ingestionJobId=job_id,
            )["ingestionJob"]
            status = job["status"]
            if status in ("COMPLETE", "FAILED"):
                break
            time.sleep(10)

        if status != "COMPLETE":
            raise RuntimeError(f"Ingestion job {job_id} ended in status {status}")

        return {"PhysicalResourceId": f"{kb_id}/{ds_id}/ingest", "Data": {"JobId": job_id, "Status": status}}

    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", f"{kb_id}/{ds_id}/ingest")}

    raise ValueError(f"Unsupported RequestType: {request_type}")
