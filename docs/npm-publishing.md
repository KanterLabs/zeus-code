# Publishing npm releases

The npm package is `@kanterlabs/zeus-code`. Pushing a stable version tag such as
`v1.0.4` runs the Python and npm checks, publishes the verified GitHub Release,
then publishes the same application version to npm with public access.
Ordinary pushes to `main` run checks without publishing a registry version.

## One-time authentication

All jobs use the organization's `homelab` runners. npm trusted publishing does
not currently support self-hosted runners, so this workflow uses a granular
access token. See [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/).

1. In npm, open your profile's **Access Tokens**, then generate a granular token.
2. Under **Packages and scopes**, select **Read and write** for `@kanterlabs`.
   The scope permits creating the first package. Organization-management access
   alone does not grant package publishing rights. After initial publication,
   you can restrict a replacement token to this package.
3. Enable **Bypass two-factor authentication** for unattended publishing and set
   an expiration. Renew the token before it expires. Keep organization management
   permissions disabled; this workflow does not need them.
4. Add the token directly to this repository's **Settings → Secrets and variables
   → Actions → New repository secret**, named **`NPM_TOKEN`**. Do not put it in a
   commit, issue or chat.

The detailed controls are documented by
[npm](https://docs.npmjs.com/creating-and-viewing-access-tokens/).

## Release and retry

Update `package.json`, `pyproject.toml` and `src/zeus_code/__init__.py` to the same
stable version, commit and push, then tag that commit:

```sh
git tag v1.0.4
git push origin v1.0.4
```

Use the new version for subsequent releases. The publisher refuses mismatched
tags and versions. It packs a verified standalone bundle and checks the registry
archive's SHA-512 integrity after publication. An existing version is skipped
only when its archive bytes match; conflicting versions fail without overwrite.

If authentication fails after the GitHub Release is created, configure or renew
`NPM_TOKEN`, then choose **Re-run failed jobs** on that tag's Actions run. The
GitHub publisher and npm publisher support this retry. Do not move an existing
tag or reuse its version for changed files. Manual workflow dispatch on `main`
only runs checks; retry the original tag run to publish.

Once published, users can run `npx @kanterlabs/zeus-code@latest` or install with
`npm install -g @kanterlabs/zeus-code`. Until the first registry publication,
`npx github:KanterLabs/zeus-code` works without npm publishing credentials.
