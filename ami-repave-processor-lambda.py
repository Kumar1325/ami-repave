import json
import boto3
import logging
from math import ceil
from os import environ
from datetime import datetime
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
def send_message_to_queue(queue_url, message_body):
    client = boto3.client("sqs")
    LOGGER.info(f"Sending message to queue with message-body: {message_body}")
    client.send_message(QueueUrl=queue_url, MessageBody=message_body)
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
def is_lt_uses_ssm_parameter(lt_id, version):
    client = boto3.client("ec2")
    response = client.describe_launch_template_versions(LaunchTemplateId=lt_id, Versions=[version])
    if ("resolve:ssm:"in response["LaunchTemplateVersions"][0]["LaunchTemplateData"]["ImageId"]):
        return response["LaunchTemplateVersions"][0]["LaunchTemplateData"]["ImageId"].split(':')[-1]
def get_asg_instances(asg_name):
    client = boto3.client("autoscaling")
    asg_instance_ids = []
    response = client.describe_auto_scaling_instances()
    while True:
        asg_instance_ids.extend([inst["InstanceId"] for inst in response["AutoScalingInstances"] if inst["AutoScalingGroupName"] == asg_name])
        if "NextToken" in response:
            response = client.describe_auto_scaling_instances(NextToken=response["NextToken"])
            asg_instance_ids.extend([inst["InstanceId"] for inst in response["AutoScalingInstances"] if inst["AutoScalingGroupName"] == asg_name])
        else:
            break
    return asg_instance_ids
def get_asg_instances_to_terminate(instance_ids, ami_id):
    client = boto3.client("ec2")
    response = client.describe_instances(InstanceIds=instance_ids)
    instances_to_terminate = [instance["InstanceId"] for instance in response["Reservations"][0]["Instances"] if instance["ImageId"] != ami_id]
    return instances_to_terminate
def lambda_handler(event, context):
    try:
        LOGGER.info("Received Event: %s", event)
        grace_days = environ.get("GracePeriodDays", 45)
        staggered_deploy = environ.get("StaggeredDeploymentPercentage", 33)
        queue_url = environ.get("AsgSqsQueueUrl")
        ami_ids_info = get_ssm_parameters_by_path(environ.get("CitiAmiIdPath", "/cti/ami/ami-ids/"))
        release_dates_info = get_ssm_parameters_by_path(environ.get("CitiAmiReleaseDatePath", "/cti/ami/release-dates/"))
        ami_map = {item["Name"]:item["Value"] for item in ami_ids_info}
        release_map = {item["Name"]:item["Value"] for item in release_dates_info}
        parameter_name_ami_id_map = {}
        for k,v in release_map.items():
            utc_now = datetime.utcnow().isoformat()
            today = datetime.strptime(utc_now.split('T')[0], "%Y-%m-%d")
            ami_release_date = datetime.strptime(v, "%Y-%m-%d")
            if (today-ami_release_date).days > int(grace_days):
                ami_id_path = f"/cti/ami/ami-ids/{k.split('/')[-1]}"
                if ami_id_path in ami_map:
                    parameter_name_ami_id_map[ami_id_path] = ami_map[ami_id_path]
        LOGGER.info(f"The following AMI-IDs release date has crossed the Grace Period days: {parameter_name_ami_id_map}")
        asgs = get_asgs()
        image_path_asg_name_map = {}
        for asg in asgs:
            if "LaunchTemplate" in asg:
                asg_image_path = is_lt_uses_ssm_parameter(asg["LaunchTemplate"]["LaunchTemplateId"],asg["LaunchTemplate"]["Version"])
                if asg_image_path and asg_image_path in parameter_name_ami_id_map:
                    asg_instances = get_asg_instances(asg["AutoScalingGroupName"])
                    instances_to_terminate = get_asg_instances_to_terminate(asg_instances, parameter_name_ami_id_map[asg_image_path])
                    if len(instances_to_terminate) > 0:
                        image_path_asg_name_map[asg[ "AutoScalingGroupName"]] = asg_image_path
        LOGGER.info(f"ASG to Repave: {image_path_asg_name_map}")
        LOGGER.info(f"Calculating % of ASGs with Staggered deployment %: {staggered_deploy}")
        repave_today_count = ceil(len(asgs) * int(staggered_deploy) / 100)
        if repave_today_count >= len(image_path_asg_name_map):
            for asg in image_path_asg_name_map:
                msg = {"Task": "ASGRefresh", "ASGName": asg}
                send_message_to_queue(queue_url, json.dumps(msg))
        else:
            for i in range(repave_today_count):
                msg = {"Task": "ASGRefresh", "ASGName": list(image_path_asg_name_map)[i]}
                send_message_to_queue(queue_url, json.dumps(msg))
    except KeyError as ke:
        LOGGER.error(f"KeyError: {ke}")
    except ClientError as ce:
        LOGGER.error(f"Client-Error: {ce}")
    except BaseException as be:
        LOGGER.error(f"Base Error: {be}")
