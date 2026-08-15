import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { CloudConfig } from './config';

/**
 * The hosted zone for the site's subdomain, and its certificate.
 *
 * Deployed FIRST, and it is the only stack with a human in the middle of it. The
 * design is subdomain DELEGATION, not zone migration:
 *
 *   - This creates a Route53 hosted zone for the subdomain alone (solar.example.com).
 *   - You add its four NS records to the PARENT zone at whatever registrar holds it.
 *   - The parent zone's own records -- apex, MX, SPF, DKIM, DMARC -- are never read,
 *     written or moved by anything here. Mail cannot break, because nothing about the
 *     parent changes except one added delegation.
 *
 * The certificate then validates by DNS inside the delegated zone, which CDK can do
 * automatically -- but only once the delegation resolves. Until the NS records are in
 * place at the registrar, `cdk deploy` on this stack WILL HANG in
 * CREATE_IN_PROGRESS on the certificate, for up to the ACM validation timeout. That
 * is not a failure and not something to retry; it is the stack waiting for you. Hence
 * the two-step deploy documented in cloud/README.md: deploy with -e to get the
 * nameservers, add them, then deploy again for the certificate.
 *
 * us-east-1 is not a preference. CloudFront can only attach a certificate from
 * us-east-1, and Lambda@Edge can only be published there, so the whole app uses that
 * one region and no stack ever references another.
 */
export interface DnsStackProps extends cdk.StackProps {
	cfg: CloudConfig;
	/**
	 * Whether to request the certificate at all. False on the first deploy, when the
	 * delegation does not resolve yet and requesting it would only hang; the stack
	 * still creates the zone, which is what produces the nameservers to delegate to.
	 */
	withCertificate: boolean;
}

export class DnsStack extends cdk.Stack {
	public readonly zone: cdk.aws_route53.HostedZone;
	public readonly certificate?: cdk.aws_certificatemanager.Certificate;

	constructor(scope: Construct, id: string, props: DnsStackProps) {
		super(scope, id, props);
		const { cfg } = props;

		this.zone = new cdk.aws_route53.HostedZone(this, 'Zone', {
			zoneName: cfg.domain,
			comment: `Delegated zone for the Sigenergy telemetry viewer (${cfg.domain})`,
		});
		// The zone outlives the stack on purpose. Deleting and recreating it issues a new
		// set of nameservers, which means editing the parent zone again -- a manual step at
		// a registrar, triggered by a `cdk destroy` that looked routine.
		this.zone.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

		if (props.withCertificate) {
			this.certificate = new cdk.aws_certificatemanager.Certificate(this, 'Certificate', {
				domainName: cfg.domain,
				validation: cdk.aws_certificatemanager.CertificateValidation.fromDns(this.zone),
			});
		}

		new cdk.CfnOutput(this, 'ZoneId', {
			value: this.zone.hostedZoneId,
			description: 'Route53 hosted zone id for the delegated subdomain',
		});
		new cdk.CfnOutput(this, 'Nameservers', {
			// Fn::Join, not a JS join: the list is only known at deploy time.
			value: cdk.Fn.join(' ', this.zone.hostedZoneNameServers ?? []),
			description:
				'Add these as NS records for the subdomain label in the PARENT zone at your ' +
				'registrar. Change nothing else there.',
		});
		if (this.certificate) {
			new cdk.CfnOutput(this, 'CertificateArn', {
				value: this.certificate.certificateArn,
				description: 'ACM certificate for the site (us-east-1, for CloudFront)',
			});
		}
	}
}
