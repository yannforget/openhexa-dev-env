# OpenHEXA skill

[`openhexa`](openhexa/SKILL.md) is a skill for coding agents. It explains how
OpenHEXA works and how to use `hexa` for common tasks: finding data, writing and
running pipelines, debugging runs, calling the API and publishing webapps.

```text
openhexa/
  SKILL.md          # start here: concepts, rules, setup, workflows
  references/
    cli.md          # CLI behaviour that is easy to get wrong
    pipelines.md    # writing pipelines with the SDK
    local-runs.md   # hexa run
    api.md          # calling the GraphQL API directly
    webapps.md      # static webapps
```

The agent reads `SKILL.md` first and opens a reference only when a task needs it.
The skill doesn't install `hexa` or set up credentials; see the
[main README](../README.md) for that.

## Use it

If your agent can read files in this repo, just ask it to:

> Read `skills/openhexa/SKILL.md`, then help me find the data I need in my
> workspace.

To make it available everywhere, copy the whole `openhexa` folder, including
`references/`, into your agent's skills folder:

```bash
cp -R skills/openhexa /path/to/agent-skills/openhexa
```

Copy it again after you update this repo. If you still have the older
`openhexa-api`, `openhexa-pipelines` or `openhexa-webapps` skills installed,
remove them: this skill replaces them.

## Maintaining the skill

- If `hexa` can explain something itself (an error hint, `--help`, `doctor`),
  improve `hexa` rather than adding text to the skill. Remove skill text that a
  CLI change makes unnecessary.
- Only keep things the agent can't find out on its own and that caused real
  mistakes. Leave out lists of options, repeated warnings and version numbers.
- Check links and commands against `--help`. Don't use a real workspace to test
  the docs.

To test a change, give the same agent the same tasks before and after, using
`-test` resources, and compare how it does: number of tool calls, whether it had
to read `hexa` or SDK source, wrong guesses, and whether it stopped to ask when
it should have. For example:

1. "Find the latest version of dataset X and download its files into `data/`."
2. "Add an optional `roots` list parameter and an output `Dataset` parameter to
   pipeline P, then run it locally against `X-test`."
3. "Why did run R fail?"
4. "Deploy pipeline P as a new pipeline." The agent should stop before deploying
   and show its plan.
