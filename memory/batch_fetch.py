import json
from pathlib import Path

from memory.batch_submit import BatchConfig


def get_job_status(job_id, config=None):
    config = config or BatchConfig()

    import volcenginesdkark
    import volcenginesdkcore

    configuration = volcenginesdkcore.Configuration()
    configuration.ak = config.ak
    configuration.sk = config.sk
    configuration.region = config.region
    configuration.client_side_validation = True
    volcenginesdkcore.Configuration.set_default(configuration)
    ark = volcenginesdkark.ARKApi(volcenginesdkcore.ApiClient(configuration))

    flt = volcenginesdkark.FilterForListBatchInferenceJobsInput(ids=[job_id])
    req = volcenginesdkark.ListBatchInferenceJobsRequest(filter=flt)
    resp = ark.list_batch_inference_jobs(req)
    items = resp.items or []
    return items[0] if items else None


def download_batch_outputs(job_id, output_prefix, results_path, errors_path, config=None):
    config = config or BatchConfig()

    import tos

    client = tos.TosClientV2(config.ak, config.sk, config.tos_endpoint, config.region)
    results_key = f"{output_prefix}{job_id}/output/results.jsonl"
    errors_key = f"{output_prefix}{job_id}/error/errors.jsonl"
    client.get_object_to_file(config.bucket, results_key, str(results_path))
    try:
        client.get_object_to_file(config.bucket, errors_key, str(errors_path))
    except Exception:
        pass


def parse_results_file(results_path):
    results_path = Path(results_path)
    parsed = []
    with results_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            custom_id = row["custom_id"]
            body = (row.get("response") or {}).get("body") or {}
            choices = body.get("choices") or []
            content = ""
            if choices:
                content = choices[0].get("message", {}).get("content", "") or ""
            payload = None
            if content:
                try:
                    payload = json.loads(content)
                except json.JSONDecodeError:
                    payload = None
            parsed.append(
                {
                    "custom_id": custom_id,
                    "payload": payload,
                    "raw_content": content,
                }
            )
    return parsed

