# Static webapps

An OpenHEXA static webapp is a set of HTML, CSS and JS files hosted at a workspace URL.
There is no server-side code and no SSR. When the app runs inside OpenHEXA, it calls
`fetch("/graphql/")` same-origin. The platform proxies the call with the viewer's
session, limited to the **scopes** granted to the app. Never embed a token in the app.

## Develop

1. Build against local fixtures first. Prefer hash routing: there is no SPA fallback.
2. To use real data during development, load the dev bridge before the app starts:

   ```html
   <script src="https://<origin>/webapps/dev.js"
           data-workspace-slug="<workspace>" data-webapp-slug="<app>"></script>
   ```

   The user approves the connection in the browser, and `window.OPENHEXA` then holds
   `{workspaceSlug, isPublic, ...}`. Handle its absence by falling back to fixtures.
   Calls made this way are real, including mutations.
3. Scopes: `USER_READ`, `PIPELINES_READ`, `PIPELINES_RUN`, `FILES_READ`,
   `DATASETS_READ`, `DATABASE_READ`. A query must fit both the app's scopes and the
   viewer's permissions.
   - Public apps cannot use the proxy.
   - For database data, use saved queries.
   - For dataset files, request a download URL through GraphQL, then fetch it without
     auth headers.

## Publish

```bash
hexa webapps create "Name" --directory dist --scope DATASETS_READ --dry-run   # new app
hexa webapps deploy APP --directory dist --dry-run                            # update
```

Review the dry run, then repeat with `--yes` once the user approves that publication.

- **`create`** publishes immediately. The app is private and has only the scopes you
  pass.
- **`deploy`** overlays files: it adds and replaces paths but never deletes remote
  files. It does not change visibility or scopes.
- **Payload limit:** about 5 MiB of encoded payload. Only the built directory is sent,
  and it needs a root `index.html`.
- **Do not upload source maps or data files unintentionally.** The publisher rejects
  known secrets, but it is not a general secret scanner.

After publishing, open the serve URL. Report what was verified in a browser separately
from what the API confirmed.
