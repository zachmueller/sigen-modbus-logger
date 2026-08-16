import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';
import { CloudConfig } from './config';
import { archiveLambdaCode } from './archive-bundle';

const REPO_ROOT = path.resolve(__dirname, '../../..');
const WEB = path.join(REPO_ROOT, 'web');

/**
 * The HTML entry points buildSite() writes with NO file extension, because they answer at
 * /view and /p/<uid> and the URL is the contract.
 *
 * Named once and shared, because the deployment has to know which objects need their
 * Content-Type declared -- see the two BucketDeployments below. A file added to buildSite()
 * without being added here would be uploaded as binary/octet-stream and would download
 * instead of rendering, which is a symptom nothing about it suggests.
 */
const HTML_ENTRY_POINTS = ['view', 'share-view'] as const;

/** Copied verbatim; their extensions tell S3 what they are. */
const WEB_ASSETS = ['app.js', 'tiles.js', 'charts.js', 'style.css', 'favicon.svg'] as const;

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
		// The same function version, plus the request body -- for /api/share ONLY, because it
		// is the only behaviour with a body and the only one behind a Lambda function URL. The
		// gate has to hash the body into `x-amz-content-sha256` or the OAC's SigV4 signature
		// does not match and Lambda refuses the request 403 without invoking it; see
		// signPayload() in cloud/lambda/auth-edge/index.js. Kept separate from `gate` so /view
		// and /agg/* are not handed a body they have no use for.
		const gateSigningBody: cdk.aws_cloudfront.EdgeLambda[] = [{
			functionVersion: edge,
			eventType: cdk.aws_cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
			includeBody: true,
		}];

		const siteOrigin = cdk.aws_cloudfront_origins.S3BucketOrigin
			.withOriginAccessControl(bucket, { originPath: '/site' });
		const dataOrigin = cdk.aws_cloudfront_origins.S3BucketOrigin
			.withOriginAccessControl(bucket);
		const authOrigin = new cdk.aws_cloudfront_origins.HttpOrigin(
			props.callbackApiDomain, {
				protocolPolicy: cdk.aws_cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
			});

		// ---- POST /api/share -------------------------------------------------
		// It lives in THIS stack, not the data stack, for two reasons. It needs no
		// cross-stack reference for its origin, which is the coupling that made the read
		// gate's code undeployable (see cdk.json). And the data stack must synth before
		// Cognito exists, while this stack already only synths once the pool ids are in
		// cloud.json.
		const shareFn = new cdk.aws_lambda.Function(this, 'ShareFn', {
			runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
			handler: 'handler.lambda_handler',
			code: archiveLambdaCode(path.join(REPO_ROOT, 'cloud/lambda/share')),
			// Copying a handful of tiles server-side. The work is S3 round trips, not CPU.
			timeout: cdk.Duration.seconds(60),
			memorySize: 512,
			environment: {
				BUCKET: cfg.bucket,
				AGG_PREFIX: 'agg/',
				SHARE_PREFIX: 'share/',
				SITE_DOMAIN: cfg.domain,
				// Reserved as TZ; see the handler.
				CAPTURE_TZ: cfg.captureTz,
			},
			logGroup: new cdk.aws_logs.LogGroup(this, 'ShareLogs', {
				retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
				removalPolicy: cdk.RemovalPolicy.DESTROY,
			}),
			description: 'Freeze one view into share/<uid>/. See cloud/lambda/share.',
		});
		// Read the aggregates, write the shares, and nothing else. Notably NOT raw/: a share
		// is made from tiles, so the archive of record is not in this function's reach.
		shareFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['s3:GetObject'],
			resources: [`arn:aws:s3:::${cfg.bucket}/agg/*`],
		}));
		shareFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['s3:PutObject', 's3:GetObject'],
			resources: [`arn:aws:s3:::${cfg.bucket}/share/*`],
		}));
		// ListBucket, for the same reason CloudFront needs it a few lines below: without it
		// S3 answers 403 for a key that does NOT exist, because it will not confirm absence
		// to a caller that cannot list. The share function has to know which tiles are
		// absent -- that is how a gap in the data is represented -- and catching 403 as
		// "absent" would make a broken policy look like an outage. Scoped by prefix, so raw/
		// cannot even be enumerated; list_objects_v2 sends a prefix, which is what makes the
		// condition work here.
		shareFn.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
			actions: ['s3:ListBucket'],
			resources: [`arn:aws:s3:::${cfg.bucket}`],
			conditions: { StringLike: { 's3:prefix': ['agg/*', 'share/*'] } },
		}));

		// AWS_IAM, not NONE. The read gate is a Lambda@Edge on a CloudFront behaviour, so
		// anything reachable BESIDE CloudFront is ungated -- and this endpoint mints public
		// copies of private telemetry, so an open function URL would let anyone with the URL
		// publish someone else's data. OAC signs CloudFront's requests with SigV4 and IAM
		// refuses everyone else, which makes the URL itself uninteresting.
		const shareUrl = shareFn.addFunctionUrl({
			authType: cdk.aws_lambda.FunctionUrlAuthType.AWS_IAM,
		});
		const shareOrigin = cdk.aws_cloudfront_origins.FunctionUrlOrigin
			.withOriginAccessControl(shareUrl);

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
				'/api/share': {
					origin: shareOrigin,
					viewerProtocolPolicy:
						cdk.aws_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
					// ALLOW_ALL because it is a POST, and CloudFront refuses methods a
					// behaviour does not list -- before the gate ever runs, so the symptom
					// would be a 403 with nothing in any log.
					//
					// A 403 with nothing in any log has a SECOND cause here, and it is the
					// one that actually happened: OAC signs this request with SigV4, and a
					// Lambda function URL rejects an unsigned payload, so a POST body must
					// arrive with its hash in `x-amz-content-sha256`. `gateSigningBody`
					// above is what puts it there. Both halves are required -- includeBody
					// without the header, or the header without includeBody, is 403.
					allowedMethods: cdk.aws_cloudfront.AllowedMethods.ALLOW_ALL,
					cachePolicy: cdk.aws_cloudfront.CachePolicy.CACHING_DISABLED,
					// The gate has to see the cookie, and the function has to see the body.
					// EXCEPT_HOST_HEADER matters for a function URL: SigV4 is computed over
					// the origin's host, so forwarding the viewer's would break the signature.
					// It also carries the header the gate adds; a policy that named headers
					// individually would have to name that one too.
					originRequestPolicy: cdk.aws_cloudfront.OriginRequestPolicy
						.ALL_VIEWER_EXCEPT_HOST_HEADER,
					edgeLambdas: gateSigningBody,
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

		// ---- what CloudFront may INVOKE -----------------------------------
		// `withOriginAccessControl()` above grants lambda:InvokeFunctionUrl and stops there.
		// That was sufficient once. Since **October 2025** a function URL requires BOTH
		// lambda:InvokeFunctionUrl and lambda:InvokeFunction, and this one was created after
		// that -- so CloudFront authenticated successfully and was then refused, which is a
		// 403 whose body is a link to urls-auth.html. Nothing invokes, so nothing logs: not in
		// the share function, and the gate one hop earlier has already said `allow`. It cost
		// two rounds of debugging. aws-cdk-lib 2.265.0 adds both actions for authType NONE
		// (aws-lambda/lib/function-url.js) and only the one for OAC, so this fills that gap;
		// a later CDK that grants it too is harmless, the statements are identical.
		//
		// SourceArn, not lambda:InvokedViaFunctionUrl, because SourceArn is what actually
		// contains this: only CloudFront acting for THIS distribution can invoke. The OAC
		// documentation's own command omits the condition key, and it is a security boundary
		// worth stating in the terms the docs use rather than improving on untested.
		shareFn.addPermission('CloudFrontInvokeFunction', {
			principal: new cdk.aws_iam.ServicePrincipal('cloudfront.amazonaws.com'),
			action: 'lambda:InvokeFunction',
			sourceArn: `arn:aws:cloudfront::${this.account}:distribution/`
				+ dist.distributionId,
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
		// TWO deployments over one prefix, and the reason is content type.
		//
		// /view and /p/<uid> must be reachable at those exact URLs, so the objects behind
		// them have NO file extension -- and BucketDeployment derives Content-Type from the
		// extension (it is `aws s3 sync` underneath, which falls back to
		// binary/octet-stream for anything it cannot recognise). The result was a browser
		// DOWNLOADING a file called `view` instead of rendering the viewer, while curl
		// reported a cheerful 200. The gate, the tiles and the page were all fine.
		//
		// contentType is per-deployment, not per-file, so the extensionless entry points
		// get their own deployment that declares it. Splitting on that boundary rather
		// than renaming to view.html is deliberate: /view already carries the Lambda@Edge
		// gate on viewer-request, and CloudFront forbids a CloudFront Function on the same
		// event type for the same behaviour, so there is nowhere to put a rewrite.
		const webSource = cdk.aws_s3_deployment.Source.asset(WEB, {
			assetHashType: cdk.AssetHashType.OUTPUT,
			bundling: {
				image: cdk.DockerImage.fromRegistry('scratch'),
				local: { tryBundle: (out: string) => buildSite(out, cfg) },
			},
		});
		const siteBucket = cdk.aws_s3.Bucket.fromBucketName(this, 'SiteBucket', cfg.bucket);

		// Everything whose extension already tells S3 what it is.
		new cdk.aws_s3_deployment.BucketDeployment(this, 'Site', {
			destinationBucket: siteBucket,
			destinationKeyPrefix: 'site',
			sources: [webSource],
			// This deployment owns pruning, so `exclude` here is load-bearing twice over:
			// it keeps the entry points out of THIS upload, and it withholds them from
			// `--delete`. Without it, whichever deployment ran last would delete the
			// other's objects.
			exclude: [...HTML_ENTRY_POINTS],
			// Only this prefix, so a deployment cannot reach raw/ or agg/.
			prune: true,
			distribution: dist,
			distributionPaths: ['/index.html', '/app.js', '/tiles.js', '/charts.js',
				'/style.css', '/favicon.svg'],
		});

		// The extensionless HTML entry points, told what they are.
		new cdk.aws_s3_deployment.BucketDeployment(this, 'SiteEntryPoints', {
			destinationBucket: siteBucket,
			destinationKeyPrefix: 'site',
			sources: [webSource],
			exclude: ['*'],
			include: [...HTML_ENTRY_POINTS],
			contentType: 'text/html; charset=utf-8',
			// The Site deployment above prunes; two deployments both passing --delete over
			// one prefix would race to remove each other's work.
			prune: false,
			distribution: dist,
			// /p/<uid> is rewritten to /share-view on viewer-request, BEFORE the cache
			// lookup, so /share-view is the cache key to invalidate.
			distributionPaths: ['/view', '/share-view'],
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

	for (const f of WEB_ASSETS) {
		fs.copyFileSync(path.join(WEB, f), path.join(out, f));
	}

	// The gated viewer. Its data comes from /agg/, which the gate also covers, and `share`
	// is what turns on the "Save this view" card -- only here. serve.py's local page has no
	// share endpoint, and a frozen share cannot re-share itself.
	fs.writeFileSync(path.join(out, 'view'),
		withSource(`window.SIGEN_SOURCE={kind:"tiles",base:"/agg/",share:"/api/share"};`));

	// A frozen share. The id comes out of the path, so one object answers for all of them,
	// and its data comes from /share/<id>/ -- public, and a copy, so re-aggregating history
	// can never change what someone was sent.
	fs.writeFileSync(path.join(out, 'share-view'), withSource(
		`(function(){var m=location.pathname.match(/^\\/p\\/([A-Za-z0-9_-]{4,64})/);`
		+ `if(!m){document.title="Not a share link";return;}`
		+ `window.SIGEN_SOURCE={kind:"tiles",base:"/share/"+m[1]+"/",frozen:true};})();`));

	fs.writeFileSync(path.join(out, 'index.html'), landingPage(cfg));

	// Every extensionless entry point must be one this stack knows to declare a Content-Type
	// for, or it deploys as binary/octet-stream and downloads instead of rendering. Checked
	// at synth, because the alternative is finding out in a browser.
	for (const f of HTML_ENTRY_POINTS) {
		if (!fs.existsSync(path.join(out, f))) {
			throw new Error(`buildSite() did not write the entry point "${f}" that `
				+ `HTML_ENTRY_POINTS promises. The two lists have drifted.`);
		}
	}
	for (const f of fs.readdirSync(out)) {
		if (!path.extname(f) && !(HTML_ENTRY_POINTS as readonly string[]).includes(f)) {
			throw new Error(`buildSite() wrote "${f}", which has no file extension and is `
				+ `not in HTML_ENTRY_POINTS. It would deploy as binary/octet-stream and a `
				+ `browser would download it instead of rendering it. Add it to that list.`);
		}
	}
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
