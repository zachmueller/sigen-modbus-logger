# The hosted viewer

The archive lives on one machine on one LAN. This is the other copy of it, and the way
three other people get to look at it.

Everything here is deploy-time only. `log.py`, `decode.py`, `series.py` and `serve.py`
stay stdlib-only Python with nothing to install — the Node toolchain in
`infrastructure/` runs on a workstation when infrastructure changes, and never on the
capture host.

## The shape of it

```
capture host  ──PutObject raw/* only──▶  S3  ──ObjectCreated──▶  ingest Lambda
                                          │                        │
                                          │   ◀── writes agg/ ─────┘
                                          ▼
                                     CloudFront  ──▶  browser
```

No persistent compute and no database. The page fetches **precomputed tiles** as static
objects, so a read costs an edge-cached GET rather than a decode: `series.py` already
keys buckets on absolute epoch and takes widths from a fixed ladder, which is what makes
per-hour tiles concatenate instead of needing re-aggregation.

One renderer. `web/app.js` reaches the outside world through a single `getJSON()` seam,
and a hosted deployment points that seam at tiles by setting `window.SIGEN_SOURCE`
before the script loads. The page here is byte-for-byte the page `serve.py` serves.

| Path | Gate |
|---|---|
| `/view` | Google sign-in, email allowlist |
| `/agg/*` (tiles) | same gate — a gate on the HTML alone would leave the data open |
| `POST /api/share` | same gate, **plus** the payload hash below — it mints public copies |
| `/p/{uid}`, `/share/*` | **public** — a shared view, copied at share time |
| `/v/{version}/*` | public, immutable — the pinned viewer bundles a share renders with |
| `/auth/*` | public; `/auth/callback` sets the cookies, `/auth/refresh` renews them |
| everything else | public: `index.html`, `app.js`, `tiles.js`, `charts.js`, `style.css` |

There is no `/login`. The gate redirects straight to the Cognito Hosted UI, so there is
nothing for such a path to do.

### The session: 30 days, and one hop a day

Three cookies, all `HttpOnly; Secure; SameSite=Lax`, all set by the callback Lambda:

| Cookie | Path | Holds |
|---|---|---|
| `sigen_id` | `/` | the Cognito id token — 24 h, and what the gate actually verifies |
| `sigen_rt` | **`/auth/`** | the refresh token, i.e. the session. 30 days |
| `sigen_sess` | `/` | epoch seconds when the id token was last minted. Not a credential |

The refresh token's `Path` is the point: a page makes hundreds of `/agg/*` fetches and this
long-lived credential rides on none of them — only on the endpoint that spends it. Which is why
there is a third cookie at all: the gate **cannot see** `sigen_rt`, so `sigen_sess` is how it
knows a refresh is worth attempting, and its value is the loop-breaker (a refresh from seconds
ago that still left no usable token cannot help, so the gate goes to Google instead).

So an expired id token is the ordinary once-a-day case, not a failure. A browser gets a 302 to
`/auth/refresh`, which spends the grant and sends it back — invisible, and the `#h=…&panels=…`
fragment survives because a browser reapplies it across a redirect whose `Location` carries none.
Google is reached about once a month, when the refresh token's 30 days are up. It does **not**
slide: Cognito's refresh grant returns no new refresh token, so the month runs from sign-in.

This replaced a 12-hour cookie wrapped around a **one-hour** id token, with the refresh token
discarded at the callback — so Google was in practice the session store and signing in was a
several-times-a-day event. See FINDINGS 33, including why a 30-day credential is affordable
here: the allowlist is re-checked at the edge on *every request*, so removing an address takes
effect immediately whatever a token's lifetime says.

**A script gets a status, never a redirect.** `/agg/*` refusals are JSON with `ok: false` and an
`error`, because a `fetch()` handed a 302 to the Hosted UI follows it cross-origin, fails CORS,
and arrives in `web/tiles.js` as "cannot reach…" — indistinguishable from a dropped connection.
The page recovers from a 401 by reloading once, which turns the fetch into the navigation that
can take the silent refresh hop. It does not recover from a 403: that means the allowlist, and
signing in again cannot change it.

**Revoking access** is `allowed_emails` plus `cdk deploy SigenAuthEdge && cdk deploy SigenSite`,
which takes effect on the next request. To kill a *session* — a stolen laptop — delete the user
from the pool (`aws cognito-idp admin-delete-user`) or rotate the app client, since the cookie
itself now lasts a month.

**`POST /api/share` bodies are signed at the edge.** The share endpoint is a Lambda function
URL with `AWS_IAM` auth, reached through Origin Access Control, and a function URL rejects an
unsigned payload — so a POST body needs its SHA-256 in `x-amz-content-sha256` or Lambda
refuses the request **403 before invoking it**, leaving nothing in the handler's log. The read
gate adds the header (`includeBody: true` on that behaviour only); nothing a client sends is
required. See `signPayload()` in `cloud/lambda/auth-edge/index.js` and FINDINGS 27.

**And CloudFront needs two invoke permissions, not one.** Since October 2025 a function URL
requires `lambda:InvokeFunction` as well as `lambda:InvokeFunctionUrl`; CDK's
`withOriginAccessControl()` grants only the second, so `site-stack.ts` adds the first itself,
scoped to this distribution by `AWS:SourceArn`. Miss it and a correctly *signed* request is
correctly *refused* — same 403, same empty log, and the only tell is that the body links to
`urls-auth.html` instead of complaining about a signature. Both grants are on the *function*;
neither is on the URL, which is why `aws lambda get-policy` is where to look.

**`/view` and `/p/{uid}` are objects with no file extension**, because the URL is the
contract. `BucketDeployment` derives `Content-Type` from the extension, so they are deployed
by a *second* deployment that declares `text/html` explicitly — see `site-stack.ts`. Get
this wrong and the browser downloads a file called `view` while `curl` reports a cheerful
`200`.

## Configuration

`cloud.json` at the repository root. **Not committed** — it holds an account id, a
domain, a Google client id and other people's email addresses, and this repository is
public. `cloud.example.json` documents every key with placeholder values and is
committed. `cdk.out/` is gitignored too: synthesized templates embed those values.

The one real secret, the Google OAuth client secret, is **never stored in this tree** — it
is passed as a `NoEcho` CloudFormation parameter and re-entered on each deploy, and the
local copy at `cloud/.google-secret` is gitignored and `chmod 600`.

It is not, however, unrecoverable: Cognito has to hold it to perform the token exchange, and
an account administrator can read it back in plaintext with

```sh
aws cognito-idp describe-identity-provider --user-pool-id <pool> --provider-name Google \
    --query 'IdentityProvider.ProviderDetails.client_secret'
```

That is unavoidable and worth knowing rather than being surprised by. It also sets the
rotation procedure: rotate in the Google console, update `cloud/.google-secret`, **and
redeploy `SigenAuthPool`** — the value in Cognito does not change by itself.

AWS credentials come from a named profile (`profile` in `cloud.json`), so nothing
sensitive sits in this tree.

## Deploying

```sh
cd cloud/infrastructure
npm install
npx cdk bootstrap aws://<account>/us-east-1 --profile <profile>
```

Everything is `us-east-1`. Not a preference: Lambda@Edge can only be published there and
CloudFront can only attach a certificate from there, so using one region throughout
means no stack ever references another across a region boundary.

### DNS: two CNAMEs, added by hand

There is no DNS stack, and no Route53 hosted zone. The site lives on a subdomain of a
zone we do not host, at a registrar (Hover) whose DNS editor **has no `NS` record type** —
so there is nothing to delegate to. Two `CNAME` records do the whole job, which is all a
subdomain on CloudFront ever needs:

| Hostname (relative to the parent) | Points at | Purpose |
|---|---|---|
| `_<token>.solar` | `_<token>.acm-validations.aws` | ACM validation. **Must stay** — ACM re-checks it on every renewal, which it attempts ~60 days before the certificate expires. Delete it and renewal fails silently then; check `NotAfter` to know when that is. |
| `solar` | `d<id>.cloudfront.net` | the site |

The parent zone's own records — apex `A`, `MX`, SPF, DKIM, DMARC, `TXT` — are never read,
written or moved by anything here. Nothing about mail or the parent's website is in the
blast radius, which is the point: migrating a zone that fronts a live site *and* Google
Workspace mail, in order to publish one subdomain, is a bad trade.

Capture the parent's records before touching anything, so there is a before/after to diff:

```sh
for t in A AAAA MX TXT CAA NS; do echo "== $t"; dig +short $t <parent-domain>; done
```

**The certificate is created out of band** and referenced by ARN in `cloud.json`:

```sh
aws acm request-certificate --domain-name <domain> --validation-method DNS \
    --key-algorithm RSA_2048 --profile <profile> --region us-east-1

aws acm describe-certificate --certificate-arn <arn> --profile <profile> \
    --region us-east-1 \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
```

Add that record at the registrar, then wait for `ISSUED`:

```sh
aws acm wait certificate-validated --certificate-arn <arn> --profile <profile> \
    --region us-east-1
```

> **Why the certificate is not a CloudFormation resource.** `AWS::CertificateManager::Certificate`
> with DNS validation blocks in `CREATE_IN_PROGRESS` until the record appears. With no
> hosted zone, CDK cannot create that record, so the deploy would sit there waiting on
> someone to open a browser — and if they took too long, roll back. An ARN in config is
> the better operational story for the one resource that genuinely has a human in the
> middle of its creation. It auto-renews for as long as the validation CNAME stays put.

### The stacks

There are **five**. Four form an ordered chain, and auth is two of them:

```sh
npx cdk deploy SigenData     --profile <profile>   # bucket, ingest Lambda, S3 events
npx cdk deploy SigenAuthPool --profile <profile> \
    --parameters "GoogleClientSecret=$(cat ../.google-secret)"   # Cognito + Google + callback
#   -> copy its UserPoolId and ClientId outputs into cloud.json
npx cdk deploy SigenAuthEdge --profile <profile>   # the read gate, with those ids baked in
npx cdk deploy SigenSite     --profile <profile>   # CloudFront + behaviours
```

The fifth, `SigenAudit`, is the CloudTrail trail and its log bucket. It is outside the
sequence because it references nothing and nothing references it, so it can be deployed at
any point — including before `SigenData`, which is arguably where it belongs, since a trail
is most useful when it predates the thing it audits. Management events only: the hourly
uploader's `PutObject` is a *data* event and does not appear, which is what keeps it
effectively free. `audit-stack.ts` explains the cost model and the one limit worth knowing
(a single-account trail cannot be tamper-proof against the account's own administrator).

`SigenAuthEdge` and `SigenSite` **do not appear in `cdk ls`** until
`cognito_user_pool_id` and `cognito_client_id` are in `cloud.json`. That is deliberate: a
Lambda@Edge cannot be given them as environment variables, so they are baked into its code
at synth — and synthesizing before the pool exists would bake the literal string
`${Token[...]}` and reject every valid login with no clue why. It is also why auth is two
stacks rather than one.

`SigenSite` prints the distribution domain. That is the target for the second `CNAME`
above, and it is the last manual step — after it propagates, the site is live.

### A share is pinned to the viewer that made it

A share copies its tiles rather than pointing at them, so re-aggregating history cannot change
what someone was sent. That was true of the data and false of the code: `/p/<uid>` served one
`site/share-view` object that loaded `/app.js`, `/charts.js` and `/style.css` from the bucket
root, and every one of those is overwritten in place by `cdk deploy SigenSite`. A link sent in
August was drawn by whatever renderer existed when it was opened.

So each deploy also publishes an immutable bundle, and each share copies that bundle's page:

```
site/v/<version>/share-view.html   the page, with its asset refs rewritten to /v/<version>/…
site/v/<version>/{app,tiles,charts}.js, style.css, favicon.svg
share/<uid>/page.html              a byte-for-byte copy of that page — what /p/<uid> serves
```

`<version>` is a 12-hex sha256 of `web/index.html` and every file it loads, computed by
`site-stack.ts`. Because it is a hash of the contents, redeploying unchanged code **overwrites
the same keys** rather than accumulating a bundle per deploy; only a real change to the page or
the renderer publishes a second one. `SigenSite` prints it as the `ViewerVersion` output, and the
share Lambda receives it as `VIEWER_VERSION` — passed in, not looked up, so which renderer a
share gets is decided by the deploy that created it.

Two things follow, and both are deliberate:

- **Old bundles are never deleted.** The `Site` deployment prunes the `site/` prefix, and `v/*`
  is excluded from it — not to keep it out of the upload, but to withhold it from `--delete`.
  Without that line, a deploy would remove the renderer every existing share names, and the
  symptom would not be a failed build: it would be links sent months ago going blank. The cost is
  about 110 KB per distinct `web/` content ever deployed, which is the price of the promise.
- **There is no prettier 404 for a bad `/p/<id>`.** CloudFront error responses are
  distribution-wide, and `web/tiles.js` reads a 404 as "no tile was published for that span, so
  the logger was off". Mapping 404 to an HTML page would hand `JSON.parse` a document and turn a
  gap in the data into a hard failure across the whole viewer.

Shares that predate this get a page written by `python3 cloud/backfill_share_pages.py`, pinned to
the bundle being deployed — which is the code they have been rendering with all along. **Run it
between the two deploys**, i.e. with the bundle published and `/p/*` still on its old route. In
that order no link is ever broken; in the other order every existing share 404s until it
finishes.

### Register the redirect URI with Google — and it is not this site

`SigenAuthPool` prints two URIs, and only the first is a manual step:

| Output | Where it goes |
|---|---|
| `GoogleAuthorizedRedirectUri` | **You** paste this into the Google OAuth client's *Authorized redirect URIs*. It is `https://<auth_domain_prefix>.auth.<region>.amazoncognito.com/oauth2/idpresponse` |
| `CognitoCallbackUrl` | Nothing. The CDK sets it on the app client. It is `https://<domain>/auth/callback` |

The browser never goes from Google to this site — it goes Google → the Cognito Hosted UI →
here. Google validates `redirect_uri` against its own list, and the only URI it is ever
asked to redirect to is Cognito's `idpresponse`. Registering the site's callback instead
produces `Error 400: redirect_uri_mismatch`, in which Google echoes the `idpresponse` URI it
expected. *Authorized JavaScript origins* is unused by a redirect flow and can be empty.

Then check the consent screen's **Publishing status**. While it is *Testing*, only accounts
on its *Test users* list can sign in, whatever `allowed_emails` says — a second gate, with
its own error (`Error 403: access_denied`, "has not completed the Google verification
process"). Publishing avoids maintaining the same addresses twice, and needs no verification
review: the app requests only `openid email profile`, all non-sensitive.

*Testing* now has a second consequence, and it is the one limit on the 30-day session above:
**Google expires refresh tokens after 7 days while an app is in Testing.** Cognito's refresh
grant for a federated user can then answer `invalid_grant`, capping the session at a week
however long the app client says. It degrades to the old behaviour rather than breaking — the
refresh endpoint clears the cookies and the next hop is one Google sign-in — but if sessions
end sooner than a month, look here first, and `refresh-refused` in the callback's log is the
line that says so.

`SigenAuthPool` and `SigenAuthEdge` refuse to synth with an empty `google_client_id` or an
empty `allowed_emails`. The second is the one worth stating: Cognito will authenticate *any*
Google account, so the allowlist is what actually gates the site — empty does not mean
"everyone" or "nobody", it means every visitor signs in successfully and is then refused.

### Changing the gate's code

`cdk deploy SigenAuthEdge && cdk deploy SigenSite`. That works only because
`cdk.json` sets `@aws-cdk/core:defaultCrossStackReferences` to `weak`; with CloudFormation
exports it is **impossible**. `SiteStack` takes the Lambda@Edge *version* ARN from
`SigenAuthEdge`, and the export name embeds the version's logical id, which embeds the
code's asset hash — so editing one line renames the export, and CloudFormation refuses to
delete the old one while the site imports it:

```
Cannot delete export SigenAuthEdge:ExportsOutputRef… as it is in use by SigenSite.
```

An export's value cannot be changed while it is imported, and the old value's resource is
gone, so there is nothing to keep alive. Weak references replace `Fn::ImportValue` with
`Fn::GetStackOutput`, which the CDK CLI resolves at deploy time and inlines — no export, so
the deadlock cannot recur. **Do not set this back to `strong`.**

**Never add `--exclusively` to either of those two deploys.** The gate's code asset is
`AssetHashType.OUTPUT` — hashed from the *bundled* output, because `config.js` is generated at
synth. `--exclusively SigenSite` does not select the edge stack, so the CLI skips bundling that
asset and hashes the raw source directory instead. The hash it gets is one that was never
built, and since the version's logical id embeds it, `SigenSite` asks for a stack output that
does not exist:

```
TemplateError: Fn::GetStackOutput references output PublishOutputRefAuthEdgeFnCurrentVersion…
from stack SigenAuthEdge, but this output was not found. The output may have been deleted.
```

Harmless — the distribution update rolls back and keeps serving the previous edge version —
but the message points at the wrong stack. `Bundling asset SigenAuthEdge/AuthEdgeFn/Fn/Code`
appearing in the output is the check that the hash is real. Without `--exclusively` the
unchanged stacks report `(no changes)` and are skipped anyway, so it buys nothing: notably
`SigenAuthPool` is *not* redeployed, and its `GoogleClientSecret` parameter is not needed.

### Rebuilding every tile — not in Lambda

```sh
python3 cloud/rebuild_tiles.py --from-s3 --plan <planhash> --invalidate
python3 cloud/rebuild_tiles.py --data-dir ~/sigen/data --invalidate    # from a local copy
```

Two jobs need this, both rare: a **backfill**, where one pass is safer than letting a burst of
S3 events race each other over the same day tile, and a **tile-format change**, where every tile
has to be rewritten and no raw file is arriving to trigger it.

It used to be `{"rebuild": "<planhash>"}` on the ingest Lambda, which now refuses that payload
and says so. A whole-archive rebuild costs time proportional to the archive — measured at ~88 s
per archive-day — so no fixed timeout holds it, and it crossed 300 s in the archive's first week.
Lambda's 900 s ceiling would have bought about six days. The failure mode is what decided it: an
unbounded job under a deadline gets killed part-way, and `ingest.run()` writes documents only
after every tile, so "part-way" means tiles rewritten and `meta.json` stale — worse than not
running. See [FINDINGS 37](../docs/FINDINGS.md).

The script imports the Lambda handler's own `_upload()` and `_rewrite_index()`, so the objects
are identical to the ones the hourly path writes, including the deliberate refusal to upload
`index.json` directly — a rebuild sees only the plans it was given, and an index built from those
alone once dropped the recovered 1 Hz series. **Pass `--invalidate`:** a finished tile is
published immutable, so CloudFront can serve the old bytes for a year (FINDINGS 30).

A change that only alters `meta.json` — `PANELS`, `ENERGY_TILES` in `serve.py` — needs none of
this. Replay one S3 event, or wait for the next rotation:

```sh
aws lambda invoke --function-name <IngestFunctionName> --cli-read-timeout 420 \
    --payload '{"Records":[{"s3":{"object":{"key":"raw/plan=<hash>/<newest>.bin.gz"}}}]}' \
    --cli-binary-format raw-in-base64-out /dev/stdout
```

## Cost

Well under **$1/month**. There is no Route53 hosted zone — DNS lives at the registrar
already paying for the parent domain — which removes what would otherwise have been the
single largest line item at $0.50. What is left: ~1 GB of compressed raw per year plus
its tiles (a few cents), an ingest Lambda that runs 24 times a day and sits well inside
the free tier, and CloudFront traffic for a handful of viewers, which does not register.
Cognito is free under 50 monthly active users; ACM certificates are free.

## What is deliberately not here

- **No live view.** Tiles appear when the logger rotates, so the hosted page is up to an
  hour behind and says so. `serve.py` on the LAN is the live one. The page now agrees: the
  *Updates* group and `Download CSV` are `data-server-only` and hidden here, because a poll
  cannot return anything new (`web/tiles.js` caches for the life of the page) and `/api/csv`
  is not a route a tile source has. A browser reload is what picks up a new batch, and the
  hint on the page says so. See FINDINGS 34.
- **No field picker below a 15-hour window.** Hour tiles carry the **32** fields the panels,
  energy tiles and live strip draw. **240** — the whole **259**-field catalogue less the 6
  counters, which travel as endpoints, and the duplicate registers — is materialised from
  `b120` up, where a tile spans a day and the per-field cost collapses. Materialising
  everything at `b30` would make a tile larger than the raw `.bin.gz` it came from.

  The window that opens it is `b120`'s, and it is **15 hours, not 30**: `choose_bucket`
  rounds *up* to the next ladder width, so `b120` is chosen once `span / TARGET_BUCKETS`
  passes the width *below* it — `60 * 900 = 54,000 s`. (`120 * 900 = 108,000 s` is 30 h,
  the *top* of the `b120` band. Quoting it as the entry point is a factor of two, and both
  this file and `web/app.js` did.) None of these numbers are asserted here: `meta.json`
  carries `fine_fields`, `coarse_fields`, `catalog`, `picker_min_bucket_s` and
  `target_buckets`, and the page derives the sentence it shows from the last two.
- **No write path to the inverter, from anywhere.** Same as the rest of the repository.
