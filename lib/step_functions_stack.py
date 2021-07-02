# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
import aws_cdk.core as cdk
import aws_cdk.aws_dynamodb as dynamodb
import aws_cdk.aws_ec2 as ec2
import aws_cdk.aws_glue as glue
import aws_cdk.aws_iam as iam
import aws_cdk.aws_lambda as _lambda
import aws_cdk.aws_lambda_event_sources as lambda_event_sources
import aws_cdk.aws_s3 as s3
import aws_cdk.aws_sns as sns
import aws_cdk.aws_stepfunctions as stepfunctions
import aws_cdk.aws_stepfunctions_tasks as stepfunctions_tasks

from .configuration import (
    AVAILABILITY_ZONES, ROUTE_TABLES, S3_RAW_BUCKET, SUBNET_IDS,
    SHARED_SECURITY_GROUP_ID, VPC_ID, get_environment_configuration,
    get_logical_id_prefix, get_resource_name_prefix
)


class StepFunctionsStack(cdk.Stack):
    def __init__(
        self, scope: cdk.Construct, construct_id: str, target_environment: str,
        raw_to_conformed_job: glue.CfnJob, conformed_to_purpose_built_job: glue.CfnJob,
        job_audit_table: dynamodb.Table,
        **kwargs
    ) -> None:
        """
        CloudFormation stack to create Step Functions, Lambdas, and SNS notification Topics

        @param scope cdk.Construct: Parent of this stack, usually an App or a Stage, but could be any construct.
        @param construct_id str:
            The construct ID of this stack. If stackName is not explicitly defined,
            this id (and any parent IDs) will be used to determine the physical ID of the stack.
        @param target_environment str: The target environment for stacks in the deploy stage
        @param raw_to_conformed_job glue.CfnJob: The glue job to invoke
        @param conformed_to_purpose_built_job glue.CfnJob: The glue job to invoke
        @param job_audit_table dynamodb.Table: The DynamoDB Table for storing Job Audit results
        @param kwargs:
        """
        super().__init__(scope, construct_id, **kwargs)

        self.mappings = get_environment_configuration(target_environment)
        logical_id_prefix = get_logical_id_prefix()
        resource_name_prefix = get_resource_name_prefix()

        vpc_id = cdk.Fn.importValue(self.mappings[VPC_ID])
        shared_security_group_output = cdk.Fn.importValue(self.mappings[SHARED_SECURITY_GROUP_ID])
        availability_zones_output = cdk.Fn.importValue(self.mappings[AVAILABILITY_ZONES])
        subnet_ids_output = cdk.Fn.importValue(self.mappings[SUBNET_IDS])
        route_tables_output = cdk.Fn.importValue(self.mappings[ROUTE_TABLES])
        # Manually construct the VPC because it lives in the target account,
        # not the Deployment Util account where the synth is ran
        vpc = ec2.Vpc.from_vpc_attributes(
            self,
            'ImportedVpc',
            vpc_id=vpc_id,
            availability_zones=availability_zones_output,
            private_subnet_ids=subnet_ids_output,
            private_subnet_route_table_ids=route_tables_output,
        )
        shared_security_group = ec2.SecurityGroup.from_security_group_id(
            self,
            'ImportedSecurityGroup',
            shared_security_group_output
        )
        raw_bucket_name = cdk.Fn.importValue(self.mappings[S3_RAW_BUCKET])
        raw_bucket = s3.Bucket.from_bucket_name(self, id='ImportedRawBucket', bucket_name=raw_bucket_name)

        notification_topic = sns.Topic(self, f'{target_environment}{logical_id_prefix}EtlFailedTopic')

        failure_function = _lambda.Function(
            self,
            f'{target_environment}{logical_id_prefix}EtlFailureStatusUpdate',
            function_name=f'{target_environment.lower()}-{resource_name_prefix}-etl-failure-status-update',
            runtime=_lambda.Runtime.PYTHON_3_8,
            handler='lambda_handler.lambda_handler',
            code=_lambda.Code.from_asset(f'{os.path.dirname(__file__)}/datalake_blog_failure_status_update'),
            environment={
                'DYNAMODB_TABLE_NAME': job_audit_table.table_name,
            },
            security_groups=[shared_security_group],
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE),
        )
        failure_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    'dynamodb:UpdateItem',
                ],
                resources=[job_audit_table.table_arn],
            )
        )
        success_function = _lambda.Function(
            self,
            f'{target_environment}{logical_id_prefix}EtlSuccessStatusUpdate',
            function_name=f'{target_environment.lower()}-{resource_name_prefix}-etl-success-status-update',
            runtime=_lambda.Runtime.PYTHON_3_8,
            handler='lambda_handler.lambda_handler',
            code=_lambda.Code.from_asset(f'{os.path.dirname(__file__)}/datalake_blog_success_status_update'),
            environment={
                'DYNAMODB_TABLE_NAME': job_audit_table.table_name,
            },
            security_groups=[shared_security_group],
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE),
        )
        success_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    'dynamodb:UpdateItem',
                ],
                resources=[job_audit_table.table_arn],
            )
        )

        fail_state = stepfunctions.Fail(
            self,
            f'{target_environment}{logical_id_prefix}EtlFailedState',
            cause='Invalid response.',
            error='Error'
        )
        success_state = stepfunctions.Succeed(self, f'{target_environment}{logical_id_prefix}EtlSucceededState')

        failure_function_task = stepfunctions_tasks.LambdaInvoke(
            self,
            f'{target_environment}{logical_id_prefix}EtlFailureStatusUpdate',
            lambda_function=failure_function,
            result_path='$.taskresult',
            retry_on_service_exceptions=True,
            output_path='$'
        )
        failure_notification_task = stepfunctions_tasks.SnsPublish(
            self,
            f'{target_environment}{logical_id_prefix}EtlFailurePublishTask',
            topic=notification_topic,
            subject='Job Completed',
            message=stepfunctions.TaskInput.from_data_at('$')
        )
        failure_function_task.next(failure_notification_task)
        failure_notification_task.next(fail_state)

        success_function_task = stepfunctions_tasks.LambdaInvoke(
            self,
            f'{target_environment}{logical_id_prefix}EtlSuccessStatusUpdate',
            lambda_function=success_function,
            result_path='$.taskresult',
            retry_on_service_exceptions=True,
            output_path='$'
        )
        success_task = stepfunctions_tasks.SnsPublish(
            self,
            f'{target_environment}{logical_id_prefix}EtlSuccessPublishTask',
            topic=notification_topic,
            subject='Job Failed',
            message=stepfunctions.TaskInput.from_data_at('$')
        )
        success_function_task.next(success_task)
        success_task.next(success_state)

        glue_raw_task = stepfunctions_tasks.GlueStartJobRun(
            self,
            f'{target_environment}{logical_id_prefix}GlueRawJobTask',
            glue_job_name=raw_to_conformed_job.name,
            arguments=stepfunctions.TaskInput.from_object({
                '--source_key.$': '$.source_key',
                '--source_system_name.$': '$.source_system_name',
                '--table_name.$': '$.table_name',
                '--source_bucketname.$': '$.source_bucketname',
                '--base_file_name.$': '$.base_file_name',
                '--p_year.$': '$.p_year',
                '--p_month.$': '$.p_month',
                '--p_day.$': '$.p_day'
            }),
            comment='Stage to Raw data load',
        )
        glue_raw_task.add_catch(failure_notification_task, result_path='$.taskresult')

        glue_conformed_task = stepfunctions_tasks.GlueStartJobRun(
            self,
            f'{target_environment}{logical_id_prefix}GlueConformedJobTask',
            glue_job_name=conformed_to_purpose_built_job.name,
            arguments=stepfunctions.TaskInput.from_object({
                '--source_key.$': '$.source_key',
                '--source_system_name.$': '$.source_system_name',
                '--table_name.$': '$.table_name',
                '--source_bucketname.$': '$.source_bucketname',
                '--base_file_name.$': '$.base_file_name',
                '--p_year.$': '$.p_year',
                '--p_month.$': '$.p_month',
                '--p_day.$': '$.p_day'
            }),
            comment='Raw to Conformed data load',
        )
        glue_conformed_task.add_catch(failure_notification_task, result_path='$.taskresult')

        machine_definition = glue_raw_task.next(
            glue_conformed_task.next(success_task)
        )

        machine = stepfunctions.StateMachine(
            self,
            f'{target_environment}{logical_id_prefix}EtlStateMachine',
            state_machine_name=f'{target_environment.lower()}-{resource_name_prefix}-etl',
            definition=machine_definition,
        )

        trigger_function = _lambda.Function(
            self,
            f'{target_environment}{logical_id_prefix}EtlTrigger',
            function_name=f'{target_environment.lower()}-{resource_name_prefix}-etl-trigger',
            runtime=_lambda.Runtime.PYTHON_3_8,
            handler='lambda_handler.lambda_handler',
            code=_lambda.Code.from_asset(f'{os.path.dirname(__file__)}/datalake_blog_trigger_load'),
            environment={
                'DYNAMODB_TABLE_NAME': job_audit_table.table_name,
                'SFN_STATE_MACHINE_ARN': machine.state_machine_arn,
            },
            security_groups=[shared_security_group],
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE),
        )
        trigger_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    'dynamodb:PutItem',
                    'dynamodb:DescribeTable',
                    'dynamodb:GetItem',
                    'dynamodb:Scan',
                    'dynamodb:Query',
                    'dynamodb:UpdateItem',
                    'dynamodb:DescribeTimeToLive',
                    'dynamodb:GetRecords',
                ],
                resources=['*'],
            )
        )
        trigger_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=['dynamodb:ListTables'],
                resources=['*'],
            )
        )
        trigger_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    'states:StartExecution',
                ],
                resources=[machine.state_machine_arn],
            )
        )
        trigger_function.add_event_source(lambda_event_sources.S3EventSource(
            bucket=raw_bucket,
            events=[
                s3.EventType.OBJECT_CREATED,
            ]
        ))