import json
import boto3
import logging
from math import ceil
from os import environ
from botocore.exceptions import ClientError
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.getLevelName(environ.get("LogLevel", "DEBUG")))
def get_ssm_parameters_by_path(parameter_path, recursive=True, with_decryption=True):
    client = boto3.client("ssm")
    parameters = []
    response = client.get_parameters_by_path(Path=parameter_path, Recursive=recursive, WithDecryption=with_decryption)
    while True:
        parameters.extend(response["Parameters"])
        if "NextToken" in response:
            response = client.get_parameters_by_path(Path=parameter_path, Recursive=recursive, WithDecryption=with_decryption, NextToken=response["NextToken"])
            parameters.extend(response["Parameters"])
        else:
            break
    return parameters
def get_ssm_parameter(parameter_name, with_decryption=True):
    client = boto3.client("ssm")
    response = client.get_parameter(Name=parameter_name, WithDecryption=with_decryption)
    return response["Parameter"]
def send_message_to_queue(sqs_queue_url, message_body):
    client = boto3.client("sqs")
    LOGGER.info(f"Sending message to queue with message-body: {message_body}")
    client.send_message(QueueUrl=sqs_queue_url, DelaySeconds=900, MessageBody=message_body)
def terminate_instances(instance_ids):
    client = boto3.client("autoscaling")
    for inst in instance_ids:
        LOGGER.info(f"Terminating the Instance: {inst}")
        client.terminate_instance_in_auto_scaling_group(InstanceId=inst, ShouldDecrementDesiredCapacity=False)
def get_asgs(asg_name=[]):
    client = boto3.client("autoscaling")
    all_asgs = []
    response = client.describe_auto_scaling_groups(AutoScalingGroupNames=asg_name)
    while True:
        all_asgs.extend(response["AutoScalingGroups"])
        if "NextToken" in response:
            response = client.describe_auto_scaling_groups(NextToken=response["NextToken"])
            all_asgs.extend(response["AutoScalingGroups"])
        else:
            break
    return all_asgs
def is_lt_uses_ssm_parameter(lt_id, version="$Default"):
    client = boto3.client("ec2")
    response = client.describe_launch_template_versions(LaunchTemplateId=lt_id, Versions=[version])
    if ("resolve:ssm:" in response["LaunchTemplateVersions"][0]["LaunchTemplateData"]["ImageId"]):
        return response["LaunchTemplateVersions"][0]["LaunchTemplateData"]["ImageId"].split(':')[-1]
def get_asg_instances(asg_name):
    client = boto3.client("autoscaling")
    asg_instance_ids = []
    response = client.describe_auto_scaling_instances()
    while True:
        asg_instance_ids.extend([inst["InstanceId"]for inst in response["AutoScalingInstances"]if (inst["AutoScalingGroupName"] == asg_name and inst["HealthStatus"].lower()=="healthy")])
        if "NextToken" in response:
            response = client.describe_auto_scaling_instances(NextToken=response["NextToken"])
            asg_instance_ids.extend([inst["InstanceId"]for inst in response["AutoScalingInstances"]if (inst["AutoScalingGroupName"] == asg_name and inst["HealthStatus"].lower()=="healthy")])
        else:
            break
    return asg_instance_ids
def get_asg_instances_to_terminate(instance_ids, ami_id):
    client = boto3.client("ec2")
    response = client.describe_instances(InstanceIds=instance_ids)
    instances_to_terminate = [instance["InstanceId"] for r in response["Reservations"] for instance in r['Instances'] if instance["ImageId"] != ami_id]
    return instances_to_terminate
def lambda_handler(event, context):
    try:
        record = json.loads(event['Records'][0]['body'].replace("\'",""))
        LOGGER.info(f"Received Message: {record}")
        asg_name = record["ASGName"]
        min_health = environ.get("MinimumHealthPercentage", 70)
        queue_url = environ.get("RepaveSqsQueueUrl")
        if record["Task"] == "ASGRefresh":
            asg_data = get_asgs(asg_name=[asg_name])
            asg_ssm_path = is_lt_uses_ssm_parameter(asg_data[0]["LaunchTemplate"]["LaunchTemplateId"],asg_data[0]["LaunchTemplate"]["Version"])
            if asg_ssm_path:
                asg_ami_id = get_ssm_parameter(asg_ssm_path)["Value"]
                if asg_ami_id.startswith("ami-"):
                    for tag in asg_data[0]["Tags"]:
                        if "Key" in tag and tag["Key"]=="MinimumHealthPercentage":
                            min_health = tag["Value"]
                    asg_instances = get_asg_instances(asg_name)
                    if len(asg_instances)==asg_data[0]["DesiredCapacity"]:
                        instances_to_terminate = get_asg_instances_to_terminate(asg_instances, asg_ami_id)
                        LOGGER.info(f"Total ASG #: {len(asg_instances)}, Intances to Terminate: {instances_to_terminate}")
                        if instances_to_terminate:
                            LOGGER.info(f"Calculating percentage of ASG Instances to Terminate with MinimumHealthPercentage: {min_health}")
                            min_health = 1 if int(min_health)==0 else min_health
                            instances_to_terminate_now = ceil(len(asg_instances) * int(min_health) / 100)
                            LOGGER.info(f"Instances to Terminate Now: {instances_to_terminate_now}")
                            if instances_to_terminate_now >= len(instances_to_terminate):
                                terminate_instances(instances_to_terminate)
                            else:
                                terminate_instances(instances_to_terminate[:instances_to_terminate_now])
                                msg = {"Task": "ASGRefresh", "ASGName": asg_name}
                                send_message_to_queue(queue_url, json.dumps(msg))
                    else:
                        LOGGER.info(f"No.of ASG running instances({len(asg_instances)}) are not equal to Desired-Capacity({asg_data[0]['DesiredCapacity']}) of ASG: {asg_name}")
                        msg = {"Task": "ASGRefresh", "ASGName": asg_name}
                        send_message_to_queue(queue_url, json.dumps(msg))
                else:
                    LOGGER.warn(f"ASG's SSM Parameter is not a valid AMI-ID: {asg_ami_id}")
            else:
                LOGGER.warn(f"ASG: {asg_name} LT not using SSM Parameter for AMI-ID")
    except KeyError as ke:
        LOGGER.error(f"KeyError: {ke}")
    except ClientError as ce:
        LOGGER.error(f"Client error: {ce}")
    except BaseException as be:
        LOGGER.error(f"Error: {be}")
