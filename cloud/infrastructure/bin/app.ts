#!/usr/bin/env node
/**
 * The CDK app. Every stack is in us-east-1, because Lambda@Edge can only be published
 * there and CloudFront can only attach a certificate from there -- so using one region
 * throughout means no stack ever references another across a region boundary.
 *
 * There is no DNS stack. The site lives on a subdomain of a zone we do not host: the
 * parent is at a registrar whose editor has no NS record type, so there is nothing to
 * delegate to. Instead the subdomain is pointed at CloudFront with a plain CNAME, and
 * the certificate is validated by a second CNAME, both added by hand once. See
 * cloud/README.md -- that is also why certificate_arn is config rather than a resource.
 *
 * Deploy order:
 *
 *   1. cdk deploy SigenData     bucket, ingest Lambda, S3 events
 *   2. cdk deploy SigenAuth     Cognito + Google + the viewer-request edge function
 *   3. cdk deploy SigenSite     CloudFront, behaviours; prints the CNAME target
 */
import * as cdk from 'aws-cdk-lib';
import { load } from '../lib/config';
import { DataStack } from '../lib/data-stack';

const cfg = load();
const app = new cdk.App();

// Tagged so every resource in the account says what put it there and that the
// definition is in git -- an account with one project in it today may not stay that way.
const commonProps: cdk.StackProps = {
	env: { account: cfg.accountId, region: cfg.region },
	tags: { Project: 'sigen-telemetry', ManagedBy: 'cdk' },
};

new DataStack(app, 'SigenData', {
	...commonProps,
	cfg,
	description: 'Telemetry bucket, ingest Lambda and the capture host credential',
});
