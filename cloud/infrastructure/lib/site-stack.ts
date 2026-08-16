import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';
import { CloudConfig } from './config';

const REPO_ROOT = path.resolve(__dirname, '../../..');
const WEB = path.join(REPO_ROOT, 'web');

/**
 * CloudFront, and the decision about what is gated.
 *
 * **The page code is public; the telemetry is not.** app.js, tiles.js, charts.js and
 * style.css are published on GitHub, so gating them would protect nothing -- and it would
 * break the public share pages, which need exactly those files to render. So the gate goes
 * on /view, /agg/* and /api/share, and nothing else.
 *
 * The one control that matters most is negative: the origin access policy names site/,
 * agg/ and share/. `raw/` is ABSENT, so the archive of record is unreachable through
 * CloudFront by any path, gated or not, however the behaviours are later edited. A
 * misconfigured behaviour can expose aggregates, which are derived and reproducible; it
 * cannot expose the raw bytes.
 *
 * Two origins over one bucket, differing only in originPath, so no URL rewriting is needed
 * for the common cases: /view resolves to site/view, /agg/x to agg/x. Only /p/* needs a
 * rewrite, because one page has to answer for every share id, and that is a CloudFront
 * Function rather than a Lambda@Edge -- it is a string operation, and those behaviours have
 * no gate to run anyway.
 */
export interface SiteStackProps extends cdk.StackProps {
	cfg: CloudConfig;
	edgeVersionArn: string;
	callbackApiDomain: string;
	shareApiDomain?: string;
}

export class SiteStack extends cdk.Stack {
	constructor(scope: Construct, id: string, props: SiteStackProps) {
		super(scope, id, props);
		const { cfg } = props;

		const bucket = cdk.aws_s3.Bucket.fromBucketName(this, 'Bucket', cfg.bucket);
		const cert = cdk.aws_certificatemanager.Certificate.fromCertificateArn(
			this, 'Cert', cfg.certificateArn);

		const edge = cdk.aws_lambda.Version.fromVersionArn(
			this, 'AuthEdge', props.edgeVersionArn);
		const gate: cdk.aws_cloudfront.EdgeLambda[] = [{
			functionVersion: edge,
			eventType: cdk.aws_cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
		}];

		const siteOrigin = cdk.aws_cloudfront_origins.S3BucketOrigin
			.withOriginAccessControl(bucket, { originPath: '/site' });
		const dataOrigin = cdk.aws_cloudfront_origins.S3BucketOrigin
			.withOriginAccessControl(bucket);
		const authOrigin = new cdk.aws_cloudfront_origins.HttpOrigin(
			props.callbackApiDomain, {
				protocolPolicy: cdk.aws_cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
			});

		// /p/<uid> -> one page for every share id. A CloudFront Function, not a
		// Lambda@Edge: it is three lines of string work on a path, it runs before the
		// cache, and it costs about a sixth as much per request.
		const shareRewrite = new cdk.aws_cloudfront.Function(this, 'ShareRewrite', {
			code: cdk.aws_cloudfront.FunctionCode.fromInline(`
function handler(event) {
  // Any /p/<anything> serves the share page. The id is read from the URL by the page
  // itself, which then fetches its data from /share/<id>/ -- a separate, public origin
  // path. Rewriting here rather than in the page means a share link has no query string
  // and no fragment to lose.
  event.request.uri = '/share-view';
  return event.request;
}`),
			comment: 'Serve the share page for any /p/<uid>',
		});

		const immutable = cdk.aws_cloudfront.CachePolicy.CACHING_OPTIMIZED;
		// The small documents change every hour and the HTML on every deploy. Honour the
		// origin's Cache-Control rather than overriding it, so ingest.py decides -- it is
		// what knows whether a span has finished.
		const respectOrigin = new cdk.aws_cloudfront.CachePolicy(this, 'RespectOrigin', {
			comment: 'Use the origin Cache-Control; ingest.py sets immutable vs 60s',
			defaultTtl: cdk.Duration.seconds(60),
			minTtl: cdk.Duration.seconds(0),
			maxTtl: cdk.Duration.days(365),
			enableAcceptEncodingGzip: true,
			enableAcceptEncodingBrotli: true,
		});

		const dist = new cdk.aws_cloudfront.Distribution(this, 'Dist', {
			domainNames: [cfg.domain],
			certificate: cert,
			// index.html is a small PUBLIC landing page, not the viewer. An anonymous
			// visitor should get something that explains what this is, rather than a
			// redirect into Google.
			defaultRootObject: 'index.html',
			httpVersion: cdk.aws_cloudfront.HttpVersion.HTTP2_AND_3,
			minimumProtocolVersion: cdk.aws_cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
			// North America and Europe only would be the cheap option and the wrong one:
			// every viewer of this site is in New Zealand.
			priceClass: cdk.aws_cloudfront.PriceClass.PRICE_CLASS_ALL,
			comment: `Sigenergy telemetry viewer (${cfg.domain})`,
			defaultBehavior: {
				// Public: the page code and the landing page. See the class docstring.
				origin: siteOrigin,
				viewerProtocolPolicy:
					cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
				cachePolicy: respectOrigin,
				compress: true,
			},
			additionalBehaviors: {
				// -- gated --------------------------------------------------
				'/view': {
					origin: siteOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					cachePolicy: cdk.aws_cloudfront.CachePolicy.CACHING_DISABLED,
					edgeLambdas: gate,
					compress: true,
				},
				'/agg/*': {
					origin: dataOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					// Gating the page and not the data would be theatre: /agg/* IS the
					// telemetry, and anyone who guessed a key could read it.
					cachePolicy: respectOrigin,
					edgeLambdas: gate,
					compress: true,
				},
				// -- public -------------------------------------------------
				'/auth/*': {
					origin: authOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					// It sets a cookie and must never be cached, and it needs the query
					// string -- the OAuth code and state arrive in it.
					cachePolicy: cdk.aws_cloudfront.CachePolicy.CACHING_DISABLED,
					originRequestPolicy: cdk.aws_cloudfront.OriginRequestPolicy
						.ALL_VIEWER_EXCEPT_HOST_HEADER,
					allowedMethods: cdk.aws_cloudfront.AllowedMethods.ALLOW_ALL,
				},
				'/p/*': {
					origin: siteOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					cachePolicy: respectOrigin,
					functionAssociations: [{
						function: shareRewrite,
						eventType: cdk.aws_cloudfront.FunctionEventType.VIEWER_REQUEST,
					}],
					compress: true,
				},
				'/share/*': {
					// A share is a deliberate, immutable copy. Public by design: that is
					// what makes a link sendable to someone with no account.
					origin: dataOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					cachePolicy: immutable,
					compress: true,
				},
			},
		});

		// ---- what CloudFront may read -------------------------------------
		// OAC generates a bucket policy automatically, but only for the prefixes it knows
		// about, and `fromBucketName` gives an IBucket the stack cannot attach one to. So
		// it is written explicitly -- which is better anyway: the list of readable prefixes
		// is the security boundary and belongs where it can be read.
		new cdk.aws_s3.CfnBucketPolicy(this, 'OacPolicy', {
			bucket: cfg.bucket,
			policyDocument: {
				Version: '2012-10-17',
				Statement: [{
					Sid: 'CloudFrontReadSitePublishedPrefixesOnly',
					Effect: 'Allow',
					Principal: { Service: 'cloudfront.amazonaws.com' },
					Action: 's3:GetObject',
					// raw/ is DELIBERATELY ABSENT. The archive of record is unreachable
					// through CloudFront at any path, however these behaviours are later
					// edited. Aggregates are derived and reproducible; the raw bytes are not.
					Resource: [
						`arn:aws:s3:::${cfg.bucket}/site/*`,
						`arn:aws:s3:::${cfg.bucket}/agg/*`,
						`arn:aws:s3:::${cfg.bucket}/share/*`,
					],
					Condition: {
						StringEquals: {
							'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/`
								+ dist.distributionId,
						},
					},
				}, {
					// So a MISSING object returns 404 rather than 403.
					//
					// Without s3:ListBucket, S3 answers AccessDenied for a key that does not
					// exist -- it will not confirm absence to a caller that cannot list. But
					// an absent tile is how web/tiles.js learns the logger was off for that
					// span: ingest.py writes nothing for a span with no records. A 403 there
					// would surface as a hard error and take the whole page down over a gap
					// in the data, and tiles.js deliberately does NOT treat 403 as absent,
					// because on a gated path a 403 is a refusal and must not read as "no
					// data".
					//
					// This grants nothing a viewer can use: CloudFront never exposes a
					// listing, and the SourceArn condition ties it to this distribution.
					Sid: 'CloudFrontDistinguishAbsentFromForbidden',
					Effect: 'Allow',
					Principal: { Service: 'cloudfront.amazonaws.com' },
					Action: 's3:ListBucket',
					Resource: `arn:aws:s3:::${cfg.bucket}`,
					Condition: {
						StringEquals: {
							'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/`
								+ dist.distributionId,
						},
						StringLike: { 's3:prefix': ['site/*', 'agg/*', 'share/*'] },
					},
				}],
			},
		});

		// ---- the page -----------------------------------------------------
		new cdk.aws_s3_deployment.BucketDeployment(this, 'Site', {
			destinationBucket: cdk.aws_s3.Bucket.fromBucketName(this, 'SiteBucket', cfg.bucket),
			destinationKeyPrefix: 'site',
			sources: [cdk.aws_s3_deployment.Source.asset(WEB, {
				assetHashType: cdk.AssetHashType.OUTPUT,
				bundling: {
					image: cdk.DockerImage.fromRegistry('scratch'),
					local: { tryBundle: (out: string) => buildSite(out, cfg) },
				},
			})],
			// Only this prefix, so a deployment cannot reach raw/ or agg/.
			prune: true,
			distribution: dist,
			distributionPaths: ['/view', '/share-view', '/index.html', '/app.js',
				'/tiles.js', '/charts.js', '/style.css'],
		});

		new cdk.CfnOutput(this, 'DistributionDomain', {
			value: dist.distributionDomainName,
			description: 'CNAME target: point the site subdomain here at your registrar',
		});
		new cdk.CfnOutput(this, 'SiteUrl', { value: `https://${cfg.domain}/view` });
	}
}

/**
 * The three HTML entry points, built from the ONE page in web/.
 *
 * They differ by a single inline script that sets window.SIGEN_SOURCE before app.js loads.
 * That is the entire difference between the local viewer, the hosted viewer and a frozen
 * share -- there is no second copy of the page, and web/index.html stays the file serve.py
 * serves, so the two cannot drift.
 */
function buildSite(out: string, cfg: CloudConfig): boolean {
	const page = fs.readFileSync(path.join(WEB, 'index.html'), 'utf-8');
	const ANCHOR = '<script src="/tiles.js"></script>';
	if (!page.includes(ANCHOR)) {
		// Loudly, at synth. A silent miss would deploy a page that renders unstyled and
		// fetches nothing, and the only symptom would be a blank viewer.
		throw new Error(`web/index.html no longer contains ${ANCHOR}, so the hosted pages `
			+ `cannot be built from it. Update site-stack.ts.`);
	}
	const withSource = (js: string) =>
		page.replace(ANCHOR, ANCHOR + '\n<script>' + js + '</script>');

	for (const f of ['app.js', 'tiles.js', 'charts.js', 'style.css', 'favicon.svg']) {
		fs.copyFileSync(path.join(WEB, f), path.join(out, f));
	}

	// The gated viewer. Its data comes from /agg/, which the gate also covers.
	fs.writeFileSync(path.join(out, 'view'),
		withSource(`window.SIGEN_SOURCE={kind:"tiles",base:"/agg/"};`));

	// A frozen share. The id comes out of the path, so one object answers for all of them,
	// and its data comes from /share/<id>/ -- public, and a copy, so re-aggregating history
	// can never change what someone was sent.
	fs.writeFileSync(path.join(out, 'share-view'), withSource(
		`(function(){var m=location.pathname.match(/^\\/p\\/([A-Za-z0-9_-]{4,64})/);`
		+ `if(!m){document.title="Not a share link";return;}`
		+ `window.SIGEN_SOURCE={kind:"tiles",base:"/share/"+m[1]+"/",frozen:true};})();`));

	fs.writeFileSync(path.join(out, 'index.html'), landingPage(cfg));
	return true;
}

/** The public landing page. Deliberately says what this is and what it is not. */
function landingPage(cfg: CloudConfig): string {
	return `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SigenStor telemetry</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
  body{font:16px/1.6 system-ui,-apple-system,sans-serif;margin:0;padding:12vh 1.5rem;
       color:#1c1c1c;background:#fbfbfa}
  main{max-width:34em;margin:0 auto}
  h1{font-size:1.4rem;margin:0 0 .25rem}
  p.sub{color:#666;margin:0 0 2rem}
  a.btn{display:inline-block;background:#1c1c1c;color:#fff;text-decoration:none;
        padding:.55rem 1.1rem;border-radius:6px;font-size:.95rem}
  ul{color:#444;padding-left:1.2em}
  footer{margin-top:3rem;color:#888;font-size:.85rem}
  @media(prefers-color-scheme:dark){body{background:#151515;color:#e8e8e8}
    p.sub,ul{color:#aaa}a.btn{background:#e8e8e8;color:#151515}footer{color:#777}}
</style></head><body><main>
  <h1>SigenStor telemetry</h1>
  <p class="sub">Read-only solar and battery data from one house, captured over Modbus.</p>
  <p><a class="btn" href="/view">Open the viewer</a></p>
  <ul>
    <li>The viewer is shared with a few specific people and needs a Google sign-in.</li>
    <li>Individual views can be shared publicly as <code>/p/&lt;id&gt;</code> links, which
        need no account.</li>
    <li>Nothing here can change anything: the capture is read-only by construction, and
        this site only ever reads what it already recorded.</li>
  </ul>
  <footer>Data is up to an hour behind — it is published when the logger rotates its
  archive. Source: <a href="https://github.com/zachmueller/sigen-modbus-logger">
  sigen-modbus-logger</a>.</footer>
</main></body></html>
`;
}
