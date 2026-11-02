const output = document.getElementById("output");
const button = document.getElementById("load");

button.addEventListener("click", async () => {
  const context = window.OPENHEXA;
  if (!context || context.isPublic) {
    output.textContent = "Local fixture: Example workspace. Connect to an approved private app to read real data (USER_READ required).";
    return;
  }
  button.disabled = true;
  try {
    const response = await fetch("/graphql/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: "query Greeting($slug: String!) { workspace(slug: $slug) { name } }",
        variables: { slug: context.workspaceSlug },
      }),
    });
    if (!response.ok) throw new Error("Request rejected");
    const result = await response.json();
    if (result.errors || !result.data?.workspace) throw new Error("Workspace unavailable");
    output.textContent = `Hello, ${result.data.workspace.name}!`;
  } catch {
    output.textContent = "Could not read workspace. Check the browser connection, USER_READ scope and viewer permissions.";
  } finally {
    button.disabled = false;
  }
});
