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
| `/d/*` (tiles) | same gate — a gate on the HTML alone would leave the data open |
| `/p/{uid}` | **public** — a shared view, copied at share time |
| `/login`, `/auth/*` | public; `/auth/callback` is what sets the cookie |

## Configuration

`cloud.json` at the repository root. **Not committed** — it holds an account id, a
domain, a Google client id and other people's email addresses, and this repository is
public. `cloud.example.json` documents every key with placeholder values and is
committed. `cdk.out/` is gitignored too: synthesized templates embed those values.

The one real secret, the Google OAuth client secret, is **never stored** — it is passed
as a `NoEcho` CloudFormation parameter and re-entered on each deploy.

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
| `_<token>.solar` | `_<token>.acm-validations.aws` | ACM validation. **Must stay** — ACM re-checks it on every renewal. |
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

```sh
npx cdk deploy SigenData --profile <profile>   # bucket, ingest Lambda, S3 events
npx cdk deploy SigenAuth --profile <profile>   # Cognito + Google + the edge function
npx cdk deploy SigenSite --profile <profile>   # CloudFront + behaviours
```

`SigenSite` prints the distribution domain. That is the target for the second `CNAME`
above, and it is the last manual step — after it propagates, the site is live.

`SigenAuth` refuses to synth with an empty `google_client_id` or an empty
`allowed_emails`. The second is the one worth stating: Cognito will authenticate *any*
Google account, so the allowlist is what actually gates the site — empty does not mean
"everyone" or "nobody", it means every visitor signs in successfully and is then
refused.

## Cost

Well under **$1/month**. There is no Route53 hosted zone — DNS lives at the registrar
already paying for the parent domain — which removes what would otherwise have been the
single largest line item at $0.50. What is left: ~1 GB of compressed raw per year plus
its tiles (a few cents), an ingest Lambda that runs 24 times a day and sits well inside
the free tier, and CloudFront traffic for a handful of viewers, which does not register.
Cognito is free under 50 monthly active users; ACM certificates are free.

## What is deliberately not here

- **No live view.** Tiles appear when the logger rotates, so the hosted page is up to an
  hour behind and says so. `serve.py` on the LAN is the live one.
- **No fine-grained field picker below 5-minute buckets.** Hour tiles carry the ~45
  fields the panels draw; the full ~259-field catalogue is materialised at `b300` and
  coarser, where the per-field cost collapses. Materialising everything at `b30` would
  make a tile larger than the raw `.bin.gz` it came from.
- **No write path to the inverter, from anywhere.** Same as the rest of the repository.
