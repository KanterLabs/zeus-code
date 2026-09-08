# Publishing npm releases

The package is `@kanterlabs/zeus-code`. Stable `v*` tags run Python and npm checks,
create the verified GitHub Release, then publish through npm trusted publishing
(OIDC). Ordinary main pushes only run checks. No `NPM_TOKEN` secret is used.

Tests and GitHub release creation run on `homelab`. The separate `npm-release`
job uses `ubuntu-24.04` because npm does not support OIDC on self-hosted runners.
This is the limited runner-policy compatibility exception. That job alone has
`id-token: write`, uses Node 24/npm 11.19.1, and disables npm caching.

## First package publication

npm requires the package to exist before configuring its trusted publisher.
This one-time bootstrap uses an interactive npm login and 2FA, not a bypass
access token. Run on a machine with Git, Node and Python 3.11+ with curses:

```sh
bootstrap_dir=$(mktemp -d "$HOME/zeus-code-publish.XXXXXX") &&
git clone --depth 1 --branch v1.0.6 https://github.com/KanterLabs/zeus-code.git "$bootstrap_dir" &&
cd "$bootstrap_dir" &&
npm run prepare &&
npm login --auth-type=web &&
npm publish --access public
```

Complete npm's browser authentication/2FA prompts. This publishes the real
v1.0.6 package. Do not publish a placeholder. If the package already exists,
skip bootstrap and configure trust below. Keep the checkout until publication
is verified; it does not affect Zeus conversations or daemon data.

## Configure npm trust once

On npm, open `@kanterlabs/zeus-code` → Settings → Trusted publishing → GitHub Actions:

| Field | Value |
| --- | --- |
| Organization or user | `KanterLabs` |
| Repository | `zeus-code` |
| Workflow filename | `ci.yml` |
| Environment | Leave blank |
| Allowed actions | Enable direct `npm publish` |

The workflow filename is just `ci.yml`, not `.github/workflows/ci.yml`. Direct
publish must be explicitly enabled; staged publishing alone is insufficient.

With npm 11.15+ and interactive authentication, the equivalent is:

```sh
npm trust github @kanterlabs/zeus-code --repo=KanterLabs/zeus-code --file=ci.yml --allow-publish
```

After a successful OIDC release, revoke the unused automation token and remove
its obsolete GitHub `NPM_TOKEN` secret. Package publishing access can disallow
token publishing. Do not disable account 2FA.

## Future releases and retries

Update `package.json`, `pyproject.toml` and `src/zeus_code/__init__.py` to the same
new stable version, commit and push, then push its matching `vX.Y.Z` tag.
The first tag containing the OIDC workflow must be newer than v1.0.6; retrying
old tags runs their original token-based workflow.

The publisher verifies the registry archive's SHA-512 integrity. It skips an
existing version only if the bytes match and refuses conflicting versions.
If trust is missing or incorrect, fix the npm configuration and choose
**Re-run failed jobs** on the new tag's Actions run. Never move an existing tag
or reuse its version for changed package contents.

Once published, users run `npx @kanterlabs/zeus-code@latest` or install using
`npm install -g @kanterlabs/zeus-code`.

Sources: [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/),
[npm trust prerequisites](https://docs.npmjs.com/cli/v11/commands/npm-trust/).
