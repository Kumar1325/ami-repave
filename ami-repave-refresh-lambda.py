from math import ceil
from os import environ
from datetime import datetime
import logging
import json
from botocore.exceptions import ClientError
import boto3


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.getLevelName(environ.get("LogLevel", "DEBUG")))


def get_ssm_parameters_by_path(parameter_path, recursive=True, with_decryption=True):
    """
    Get SSM Parameter

    Arguments:
    parameter_path(string): SSM Parameter Path

    Returns: SSM Parameter Output(dict)
    """
    client = boto3.client("ssm")
    response = client.get_parameters_by_path(
        Path=parameter_path, Recursive=recursive, WithDecryption=with_decryption
    )
    return response["Parameters"]


def get_ssm_parameter(parameter_name, with_decryption=True):
    """
    Get SSM Parameter

    Arguments:
    parameter_name(string): SSM Parameter name

    Returns: SSM Parameter Output(dict)
    """
    client = boto3.client("ssm")
    response = client.get_parameters(
        Name=parameter_name, WithDecryption=with_decryption
    )
    return response["Parameter"]


def send_message_to_queue(sqs_queue_url, message_body):
    """
    Send Message to SQS Queue

    Arguments:
    sqs_queue_url(str): SQS Queue URL
    message_body(str): SQS queue message body

    Returns: None
    """
    client = boto3.client("sqs")
    client.send_message_to_queue(QueueUrl=sqs_queue_url, MessageBody=message_body)


def delete_message_from_queue(sqs_queue_url, receipt_handle):
    """
    Deletes Message from SQS Queue
    Arguments:
    sqs_queue_url(str): SQS Queue URL
    receipt_handle(str): Message Receipt handle
    """
    client = boto3.client("sqs")
    client.detete_message(QueueUrl=sqs_queue_url, ReceiptHandle=receipt_handle)


def terminate_instances(instance_ids):
    """
    Terminates the Provides list of EC2 instances
    Arguments:
    instance_ids(list): Instance-Ids list
    """
    client = boto3.client("ec2")
    LOGGER.info("Terminating the following instances: %s",instance_ids)
    client.terminate_instances(InstanceIds=instance_ids)


def get_asgs(asg_name=[]):
    """
    Get all ASGs in the account
    Arguments:
    region(string): AWS region, defaults to us-east-2 (Ohio)
    Retruns: asgs(list)
    """
    client = boto3.client("autoscaling")
    all_asgs = []
    response = client.describe_auto_scaling_groups(AutoScalingGroupNames=asg_name)
    while True:
        all_asgs.extend(response["AutoScalingGroups"])
        if "NextToken" in response:
            response = client.describe_auto_scaling_groups(
                NextToken=response["NextToken"]
            )
            all_asgs.extend(response["AutoScalingGroups"])
        else:
            break
    return all_asgs


def is_lt_uses_ssm_parameter(lt_id, version="$Default"):
    """
    Get all Default Launch Template Versions in the account
    Arguments:
    lt_id(str): Launch Template Id
    version(int): Launch Template version number
    Retruns: boolean
    """
    client = boto3.client("ec2")
    response = client.describe_launch_template_versions(
        LaunchTemplateId=lt_id, Versions=[version]
    )
    if (
        "resolve:ssm:"
        in response["LaunchTemplateVersions"][0]["LaunchTemplateData"]["ImageId"]
    ):
        return response["LaunchTemplateVersions"][0]["LaunchTemplateData"]["ImageId"]


def get_asg_instances(asg_name):
    """
    Get instances in ASG

    Arguments:
    asg_name(str): Auto Scaling Group Name

    Return: asg_instance_ids(list)
    """
    client = boto3.client("autoscaling")
    asg_instance_ids = []
    response = client.describe_auto_scaling_instances()
    while True:
        asg_instance_ids.extend(
            [
                inst["InstanceId"]
                for inst in response["AutoScalingInstances"]
                if inst["AutoScalingGroupName"] == asg_name
            ]
        )
        if "NextToken" in response:
            response = client.describe_auto_scaling_instances(
                NextToken=response["NextToken"]
            )
            asg_instance_ids.extend(
                [
                    inst["InstanceId"]
                    for inst in response["AutoScalingInstances"]
                    if inst["AutoScalingGroupName"] == asg_name
                ]
            )
        else:
            break
    return asg_instance_ids


def get_asg_instances_to_terminate(instance_ids, ami_id):
    """
    Get instances in ASG to which are not using the provide ami_id

    Arguments:
    instance_ids(list): Auto Scaling Group Name
    ami_id(str): Latest AMI-Id which is configured to be used by ASG.

    Return: instances_to_terminate(list)
    """
    client = boto3.client("ec2")
    response = client.describe_instances(InstanceIds=instance_ids)
    instances_to_terminate = [
        instance["InstanceId"]
        for instance in response["Reservations"][0]["Instances"]
        if instance["ImageId"] != ami_id
    ]
    return instances_to_terminate


def lambda_handler(event, context):
    """
    The Lambda Handler, execution starts from here

    Arguments:
    event(dict): Event information
    context(dict): Lambda execution context

    Returns: None
    """
    try:
        LOGGER.info("Received Event: %s", event)
        # Get - Lambda Environmet Variables
        grace_days = environ.get("GracePeriodDays", 45)
        staggered_deploy = environ.get("StaggeredDeploymentPercentage", 33)
        sqs_queue_url = environ.get("AsgSqsQueueUrl")

        if ("source" in event) and (event["source"] == "aws.events"):
            ami_ids_path = environ.get("CitiAmiIdPath", "/cti/base/ami-ids/")
            ami_ids_info = get_ssm_parameters_by_path(ami_ids_path)
            parameter_name_ami_id_map = {}
            for ami in ami_ids_info:
                ami_release_date = ami["LastModifiedDate"]
                delta = datetime.utcnow() - ami_release_date.replace(tzinfo=None)
                if delta.days >= grace_days:
                    parameter_name_ami_id_map[ami["Name"]] = ami["Value"]
            LOGGER.info(
                "The following AMI-IDs release date has crossed the Grace Period days: %s",
                parameter_name_ami_id_map,
            )

            asgs = get_asgs()
            image_path_asg_name_map = {}
            for asg in asgs:
                if "LaunchTemplate" in asg:
                    asg_image_path = is_lt_uses_ssm_parameter(
                        asg["LaunchTemplate"]["LaunchTemplateId"],
                        asg["LaunchTemplate"]["Version"],
                    )
                    if asg_image_path and asg_image_path in parameter_name_ami_id_map:
                        asg_instances = get_asg_instances(asg["AutoScalingGroupName"])
                        instances_to_terminate = get_asg_instances_to_terminate(
                            asg_instances, parameter_name_ami_id_map[asg_image_path]
                        )
                        if len(instances_to_terminate) > 0:
                            image_path_asg_name_map[asg_image_path] = asg[
                                "AutoScalingGroupName"
                            ]
                    else:
                        LOGGER.info(
                            "Autoscaling group: %s is not using the SSM Parameter ImageId",
                            asg['AutoScalingGroupName']
                        )
                else:
                    LOGGER.info(
                        "Autoscaling group: %s is not using Launch Template",
                        asg["AutoScalingGroupName"],
                    )
            LOGGER.info("ASG to Repave: %s", image_path_asg_name_map)
            LOGGER.info("Calculating % of ASGs for Staggered deployment")
            repave_today_count = ceil(len(asgs) * staggered_deploy / 100)
            if repave_today_count > len(image_path_asg_name_map):
                for asg in image_path_asg_name_map:
                    msg = {"Task": "ASGRefresh", "ASGName": asg}
                    LOGGER.info("Sending message to queue with message-body: %s", msg)
                    send_message_to_queue(sqs_queue_url, json.dumps(msg))
            else:
                for i in range(repave_today_count):
                    msg = {"Task": "ASGRefresh", "ASGName": image_path_asg_name_map[i]}
                    LOGGER.info("Sending message to queue with message-body: %s", msg)
                    send_message_to_queue(sqs_queue_url, json.dumps(msg))

        if "Records" in event and (event["Records"][0]["eventSource"] == "aws.sqs"):
            record = json.loads(event["Records"][0]["body"])
            LOGGER.info("Received Message: %s", asg_name)
            asg_name = record["ASGName"]
            min_health = environ.get("MinimumHealthPercentage", 70)
            if record["Task"] == "ASGRefresh":
                asg_data = get_asgs(asg_name=[asg_name])
                for tag in asg_data[0]["Tags"]:
                    if "MinimumHealthPercentage" in tag:
                        min_health = tag["MinimumHealthPercentage"]
                asg_ssm_path = is_lt_uses_ssm_parameter(
                    asg_data[0]["LaunchTemplate"]["LaunchTemplateId"],
                    asg_data[0]["LaunchTemplate"]["Version"],
                )
                asg_ami_id = get_ssm_parameter(asg_ssm_path)["Value"]
                if asg_ami_id.startswith("ami-"):
                    asg_instances = get_asg_instances(asg_name)
                    instances_to_terminate = get_asg_instances_to_terminate(
                        asg_instances, asg_ami_id
                    )
                    LOGGER.info("Calculating percentage of ASG Instances to Terminate")
                    instances_to_terminate_today = ceil(
                        len(asg_instances) * min_health / 100
                    )
                    if instances_to_terminate_today > len(instances_to_terminate):
                        terminate_instances(instances_to_terminate)
                    else:
                        terminate_instances(
                            instances_to_terminate[:instances_to_terminate_today]
                        )
                else:
                    LOGGER.info(
                        "ASG's SSM Parameter is not a valid AMI-ID: %s", asg_ami_id
                    )

    except KeyError as key_eror:
        error_message = f"AMI-Repave-Lambda: failed with KeyError: {key_eror}"
        LOGGER.error(error_message)
        raise Exception(error_message)
    except ClientError as client_error:
        error_message = (
            f"AMI-Repave-Lambda: failed with boto3 Client error: {client_error}"
        )
        LOGGER.error(error_message)
        raise Exception(error_message)
    except BaseException as base_error:
        error_message = f"AMI-Repave-Lambda: failed with error: {base_error}"
        LOGGER.error(error_message)
        raise Exception(error_message)
