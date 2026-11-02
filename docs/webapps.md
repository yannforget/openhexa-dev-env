# Webapps

`hexa` publishes static webapps: a folder of HTML, JS and CSS with an
`index.html` at its root. Build the app with your usual tools, then give `hexa`
the output folder. `hexa` doesn't build anything, and static apps can't have a
backend.

## Publish

Create a new app:

```bash
hexa webapps create "My dashboard" --directory ./dist --dry-run
hexa webapps create "My dashboard" --directory ./dist --yes
```

Update an existing one:

```bash
hexa webapps deploy my-dashboard --directory ./dist --dry-run
hexa webapps deploy my-dashboard --directory ./dist --yes
```

Both commands publish the files immediately.

A deploy adds new files and replaces existing ones, **but does not delete files**.
A file you delete locally remains published. This matters for old data files you
no longer want to publish.

The whole upload must stay under 5 MiB. `.env` files, `.git`, dependency folders
and files containing your token are refused, but this is not a complete secret scan.
Look at what's in the folder before you publish, including source maps.

## Access to OpenHEXA data

New apps are private and can't call the OpenHEXA API. To let an app read data,
give it scopes when you create it:

```bash
hexa webapps create "My dashboard" --directory ./dist \
  --scope DATASETS_READ --scope PIPELINES_READ --yes
```

Available scopes: `USER_READ`, `PIPELINES_READ`, `PIPELINES_RUN`, `FILES_READ`,
`DATASETS_READ`, `DATABASE_READ`. Request only the scopes the app needs. `PIPELINES_RUN` lets
the app start real runs.

Deploying content doesn't change the app's scopes or visibility.

## Develop locally

The app calls `fetch('/graphql/')` on its own origin, without any token. Requests
use the session of the person viewing the app.

To work against real data during development, add this script before your app
starts (change the URL and slugs):

```html
<script src="https://app.openhexa.org/webapps/dev.js"
        data-workspace-slug="my-workspace" data-webapp-slug="my-dashboard"></script>
```

The browser asks you to approve the connection. This is not a test mode: actions
such as starting a pipeline have real effects.

To develop without connecting to OpenHEXA, copy the
[example app](../examples/webapp/), which uses local sample data.

For more detail, see the [webapps reference](../skills/openhexa/references/webapps.md)
in the agent skill.
