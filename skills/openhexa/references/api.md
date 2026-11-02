# Direct GraphQL API

Use the API for operations that `hexa` does not support. Everything is a POST to
`<origin>/graphql/` with `Authorization: Bearer <token>` and the body
`{"query": ..., "variables": ...}`.

## Client

The simplest client is `hexa`'s own transport, which reads the same `.env`. It keeps
TLS verification on, refuses redirects, sets timeouts and checks mutation results:

```python
from pathlib import Path
from hexa.api.client import Transport
from hexa.config import load_credentials

creds = load_credentials(Path(".env"))  # .origin, .workspace, .token
api = Transport(creds)
data = api.graphql(
    "query($ws:String!){workspace(slug:$ws){name}}", {"ws": creds.workspace}
)
result = api.mutate(
    "createDatasetVersion",
    "CreateDatasetVersionInput",
    {"datasetId": "...", "name": "v2"},
    "version { id name }",
)
```

Run it with the interpreter that has `hexa` installed: `head -1 "$(command -v hexa)"`
prints its path. `mutate` sends the request **once**. It raises `UNCERTAIN_WRITE` when
the outcome is unknown and `WRITE_REJECTED` when the server returns `success: false`.

With any other client, apply the same checks:
- HTTP 200 is not success. Check top-level `errors`, and each mutation's `success` and
  `errors`.
- Never retry a mutation automatically.
- Never send the bearer token to a signed storage URL.

## Schema

Look up only the type you need on the target itself:
`hexa api type CreateDatasetVersionInput` lists its fields, types and descriptions.
The upstream schema is in `openhexa-app` under `backend/hexa/*/graphql/schema.graphql`.
It may be newer than the deployed server.

## Useful entry points

| Need | Query / mutation |
| --- | --- |
| Dataset links in a workspace | `workspace(slug){datasets(page,perPage){items{id dataset{id slug workspace{slug}}}}}`; outer `id` is the link ID |
| Dataset versions/files | `dataset(id){versions{...}}`, `datasetVersion(id){files(page,perPage){items{id filename uri}}}` |
| Bucket objects | `workspace(slug){bucket{objects(prefix,page,perPage){items{key size} hasNextPage}}}` |
| Download bucket object | `prepareObjectDownload({workspaceSlug, objectKey})` → `downloadUrl` |
| Upload bucket object (overwrites) | `prepareObjectUpload({workspaceSlug, objectKey, contentType})` → PUT to `uploadUrl` with `headers` |
| Delete bucket object | `deleteBucketObject({workspaceSlug, objectKey})` |
| Share dataset to workspace | `linkDataset({datasetId, workspaceSlug})` |
| Unlink / delete | `deleteDatasetLink({id: linkId})`, `deleteDatasetVersion({versionId})`, `deleteDataset({id})` |
| Add file to latest version | `generateDatasetUploadUrl({versionId, uri, contentType})` → PUT → `createDatasetVersionFile({versionId, uri, contentType})` |

Things to know:
- Only the latest dataset version accepts new files.
- Existing dataset files cannot be overwritten.
- No mutation deletes a single dataset file. To drop one, publish a new version without
  it.
- Deleting a dataset affects every workspace it is linked to.
