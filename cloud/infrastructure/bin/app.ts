#!/usr/bin/env node
/**
 * The CDK app. Every stack is in us-east-1, because Lambda@Edge can only be published
 * there and CloudFront can only attach a certificate from there -- so using one region
 * throughout means no stack ever references another across a region boundary.
 *
 * There is no DNS stack: the site lives on a subdomain of a zone we do not host, whose
 * registrar has no NS record type, so it is pointed here with two hand-added CNAMEs. See
 * cloud/README.md, which is also why certificate_arn is config rather than a resource.
 *
 * Deploy order. Two of the steps have a human in the middle, and both are there because
 * something cannot be known until something else exists:
 *
 *   1. cdk deploy SigenData        bucket, ingest Lambda, S3 events
 *   2. cdk deploy SigenAuthPool    Cognito + Google + the callback
 *      -> put its UserPoolId and ClientId in cloud.json
 *   3. cdk deploy SigenAuthEdge    the read gate, with those ids baked in
 *   4. cdk deploy SigenSite        CloudFront; prints the CNAME target
 *      -> point the subdomain at it
 *
 * Steps 2 and 3 are separate because a Lambda@Edge cannot have environment variables and
 * asset bundling happens before CloudFormation resolves tokens; auth-stack.ts explains it
 * at length. Step 2 needs the Google client secret:
 *
 *   cdk deploy SigenAuthPool \
 *     --parameters GoogleClientSecret="$(cat ../.google-secret)"
 */
import * as cdk from 'aws-cdk-lib';
import { load } from '../lib/config';
import { DataStack } from '../lib/data-stack';
import { AuthPoolStack, AuthEdgeStack } from '../lib/auth-stack';
import { SiteStack } from '../lib/site-stack';

const cfg = load();
const app = new cdk.App();

// Tagged so every resource in the account says what put it there and that the
// definition is in git -- an account with one project in it today may not stay that way.
const commonProps: cdk.StackProps = {
	env: { account: cfg.accountId, region: cfg.region },
	tags: { Project: 'sigen-telemetry', ManagedBy: 'cdk' },
};

new DataStack(app, 'SigenData', {
	...commonProps, cfg,
	description: 'Telemetry bucket, ingest Lambda and the capture host credential',
});

const pool = new AuthPoolStack(app, 'SigenAuthPool', {
	...commonProps, cfg,
	description: 'Cognito user pool, Google identity provider and the OAuth callback',
});

// Only synthesized once the pool's ids are in cloud.json. Guarded rather than skipped, so
// `cdk deploy SigenAuthEdge` before step 2 fails with an explanation instead of building a
// gate that rejects every valid token.
const edge = cfg.cognitoUserPoolId && cfg.cognitoClientId
	? new AuthEdgeStack(app, 'SigenAuthEdge', {
		...commonProps, cfg,
		description: 'Viewer-request read gate: verified Google identity plus an allowlist',
	})
	: null;

if (edge) {
	new SiteStack(app, 'SigenSite', {
		...commonProps, cfg,
		edgeVersionArn: edge.edgeVersion.edgeArn,
		callbackApiDomain: pool.callbackApiDomain,
		description: 'CloudFront distribution, behaviours and the published page',
	});
}
