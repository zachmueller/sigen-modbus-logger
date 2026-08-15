#!/usr/bin/env node
/**
 * The CDK app. Every stack is in us-east-1, because Lambda@Edge can only be published
 * there and CloudFront can only attach a certificate from there -- so using one region
 * throughout means no stack ever references another across a region boundary.
 *
 * Deploy order matters, and it is not just dependency order: DnsStack has a human in
 * the middle of it. See cloud/README.md.
 *
 *   1. cdk deploy SigenDns                     creates the zone, prints nameservers
 *   2. (you add those NS records at the registrar)
 *   3. cdk deploy SigenDns -c cert=1           requests + validates the certificate
 *   4. cdk deploy SigenData                    bucket, ingest, tiles
 *   5. cdk deploy SigenAuth                    Cognito + Google + the edge function
 *   6. cdk deploy SigenSite                    CloudFront, behaviours, alias record
 *
 * `-c cert=1` exists because requesting the certificate before the delegation resolves
 * does not fail -- it HANGS in CREATE_IN_PROGRESS until ACM gives up, which reads like
 * a broken deploy rather than a wait. Better to not ask for it yet.
 */
import * as cdk from 'aws-cdk-lib';
import { load } from '../lib/config';
import { DnsStack } from '../lib/dns-stack';

const cfg = load();
const app = new cdk.App();

const env = { account: cfg.accountId, region: cfg.region };

// Tagged so every resource in the account says what put it there and that the
// definition is in git -- an account with one project in it today may not stay that way.
const tags = { Project: 'sigen-telemetry', ManagedBy: 'cdk' };

new DnsStack(app, 'SigenDns', {
	env,
	tags,
	cfg,
	// Truthy only when explicitly asked for: see the header.
	withCertificate: !!app.node.tryGetContext('cert'),
	description: `Delegated Route53 zone and ACM certificate for ${cfg.domain}`,
});
