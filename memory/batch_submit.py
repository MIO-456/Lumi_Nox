import os
from pathlib import Path


def load_env(env_path="D:/AI_Streamer/.env"):
    env = {}
    path = Path(env_path)
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


class BatchConfig:
    def __init__(self, env=None):
        env = env or load_env()
        self.region = env.get("TOS_REGION", "cn-beijing")
        self.tos_endpoint = env.get("TOS_ENDPOINT", "tos-cn-beijing.volces.com")
        self.bucket = env.get("TOS_BUCKET", "lumi-seedance-assets")
        self.ak = env.get("TOS_ACCESS_KEY") or os.environ.get("VOLC_ACCESSKEY")
        self.sk = env.get("TOS_SECRET_KEY") or os.environ.get("VOLC_SECRETKEY")


def submit_batch_file(local_input, input_object_key, output_object_key, model_name, model_version, job_name, project_name="default", config=None):
    config = config or BatchConfig()
    if not config.ak or not config.sk:
        raise ValueError("missing TOS credentials")

    import tos
    import volcenginesdkark
    import volcenginesdkcore

    client = tos.TosClientV2(config.ak, config.sk, config.tos_endpoint, config.region)
    client.put_object_from_file(config.bucket, input_object_key, str(local_input))
    client.put_object(config.bucket, output_object_key, content=b"")

    configuration = volcenginesdkcore.Configuration()
    configuration.ak = config.ak
    configuration.sk = config.sk
    configuration.region = config.region
    configuration.client_side_validation = True
    volcenginesdkcore.Configuration.set_default(configuration)
    ark = volcenginesdkark.ARKApi(volcenginesdkcore.ApiClient(configuration))

    req = volcenginesdkark.CreateBatchInferenceJobRequest(
        input_file_tos_location=volcenginesdkark.InputFileTosLocationForCreateBatchInferenceJobInput(
            bucket_name=config.bucket,
            object_key=input_object_key,
        ),
        model_reference=volcenginesdkark.ModelReferenceForCreateBatchInferenceJobInput(
            foundation_model=volcenginesdkark.FoundationModelForCreateBatchInferenceJobInput(
                model_version=model_version,
                name=model_name,
            ),
        ),
        name=job_name,
        output_dir_tos_location=volcenginesdkark.OutputDirTosLocationForCreateBatchInferenceJobInput(
            bucket_name=config.bucket,
            object_key=output_object_key,
        ),
        project_name=project_name,
    )
    return ark.create_batch_inference_job(req).id
