/** /self-host: DEPLOY.md as a page. Keep the two in step. */

import { GITHUB_URL } from "../../components/site/SiteChrome";

const API_SETTINGS: [string, string, string][] = [
  ["GEMINI_API_KEY / ANTHROPIC_API_KEY", "—", "LLM keys; at least one. With only a Gemini key, Gemini is used; with both, Claude is."],
  ["GITHUB_TOKEN", "—", "GitHub token for pull request and issue lookups (5,000 requests an hour instead of 60)."],
  ["ARCH_ACCESS_CODE", "— (Render generates one)", "The code visitors must enter. Empty means anyone can use your keys."],
  ["ARCH_PROVIDER", "from the keys", "gemini or anthropic."],
  ["ARCH_MODEL / ARCH_GRADER_MODEL", "provider default", "The model for every step, or for relevance grading only."],
  ["ARCH_RUNS_PER_HOUR", "20", "Questions per visitor address per hour (0 = no limit)."],
  ["ARCH_MAX_REPO_MB", "500 (Render: 300)", "The largest GitHub repository it will clone."],
  ["ARCH_CORS_ORIGINS", "*", "Websites allowed to call the API, comma-separated."],
  ["ARCH_EMBEDDER", "FastEmbed (Render: none)", "none = keyword search only, which needs less memory."],
  ["ARCH_WEB_ALLOW_LOCAL", "0", "The container opens GitHub repositories only."],
  ["ARCH_HOME", "/data", "Where clones, indexes and question history are kept."],
  ["PORT", "8765", "Port to listen on (Render sets it)."],
];

function Code({ children }: { children: string }) {
  return (
    <pre className="code-block">
      <code>{children}</code>
    </pre>
  );
}

export function SelfHost() {
  return (
    <main id="content" className="site">
      <header className="page-head container">
        <p className="eyebrow">Self-host</p>
        <h1 className="page-head__title">Run it yourself</h1>
        <p className="page-head__lead">
          On your own machine it's one script. On a server it's two parts: the API, a Docker container that clones
          repositories and runs each question for a minute or three, and the website, which can live on Vercel or be
          served by the API itself. Everything is set with environment variables; nothing secret goes in the
          repository or the image.
        </p>
      </header>

      <div className="container doc">
        <section aria-labelledby="sh-local">
          <h2 id="sh-local" className="lp-h2">
            On your machine
          </h2>
          <p>
            You need Python 3.11+, <a href="https://docs.astral.sh/uv/">uv</a>, git and Node.js. Clone{" "}
            <a href={GITHUB_URL}>the repository</a>, then:
          </p>
          <Code>{"./start.sh   # installs everything, builds the website, opens http://localhost:8765"}</Code>
          <p>
            The first run creates <code>.env</code>. Put a key in it: <code>GEMINI_API_KEY</code> (free tier,{" "}
            <a href="https://aistudio.google.com/apikey">get one</a>) or <code>ANTHROPIC_API_KEY</code>. Locally you
            can open any clone on your disk as well as GitHub repositories. <code>./start.sh ask &lt;repo&gt;</code>{" "}
            works in the terminal without the website.
          </p>
        </section>

        <section aria-labelledby="sh-render">
          <h2 id="sh-render" className="lp-h2">
            The API on Render
          </h2>
          <ol className="doc-steps">
            <li>Push the repository to GitHub. A private repository is fine; <code>.env</code> is git-ignored.</li>
            <li>
              In Render, choose <strong>New → Blueprint</strong> and pick the repository. Render reads{" "}
              <code>render.yaml</code>.
            </li>
            <li>
              Fill in what it asks for: <code>GEMINI_API_KEY</code> and/or <code>ANTHROPIC_API_KEY</code>, and
              optionally <code>GITHUB_TOKEN</code> (a fine-grained token with <em>Public repositories (read-only)</em>{" "}
              and no other permissions).
            </li>
            <li>
              Apply. When it's live, note the service URL, copy the generated <code>ARCH_ACCESS_CODE</code> from{" "}
              <strong>Environment</strong>, and check that <code>/api/health</code> answers <code>{'{"ok":true}'}</code>.
            </li>
          </ol>
          <p>The Render URL already serves the whole website. If that's all you need, you're done.</p>
        </section>

        <section aria-labelledby="sh-vercel">
          <h2 id="sh-vercel" className="lp-h2">
            The website on Vercel
          </h2>
          <ol className="doc-steps">
            <li>In Vercel, add a project from the same repository.</li>
            <li>
              Set <strong>Root Directory</strong> to <code>frontend</code>. The build settings come from{" "}
              <code>frontend/vercel.json</code>.
            </li>
            <li>
              Add the environment variable <code>VITE_API_URL</code>: your API's URL, without a trailing slash, for
              example <code>https://repo-archaeologist-api.onrender.com</code>.
            </li>
            <li>Deploy, open the site, and enter the access code.</li>
          </ol>
          <p>
            <code>VITE_API_URL</code> is built into the website, so changing it needs a redeploy. To let only your
            website call the API, set <code>ARCH_CORS_ORIGINS</code> on the API to your Vercel URL; the access code
            is what protects it either way.
          </p>
        </section>

        <section aria-labelledby="sh-docker">
          <h2 id="sh-docker" className="lp-h2">
            Anywhere Docker runs
          </h2>
          <Code>
            {`docker build -t repo-archaeologist .
docker run -p 8765:8765 -v archaeologist-data:/data \\
  -e GEMINI_API_KEY=... -e GITHUB_TOKEN=... -e ARCH_ACCESS_CODE=choose-one \\
  repo-archaeologist`}
          </Code>
          <p>
            Fly.io, Railway, Google Cloud Run and a plain VPS all work. On Cloud Run, raise the request timeout (for
            example <code>--timeout=3600</code>), because progress streams for minutes. Mount a volume at{" "}
            <code>/data</code> wherever the host offers one.
          </p>
        </section>

        <section aria-labelledby="sh-settings">
          <h2 id="sh-settings" className="lp-h2">
            Settings
          </h2>
          <div className="table-wrap">
            <table className="settings">
              <caption className="visually-hidden">API settings</caption>
              <thead>
                <tr>
                  <th scope="col">Variable</th>
                  <th scope="col">Container default</th>
                  <th scope="col">What it does</th>
                </tr>
              </thead>
              <tbody>
                {API_SETTINGS.map(([name, def, what]) => (
                  <tr key={name}>
                    <th scope="row">
                      <code>{name}</code>
                    </th>
                    <td>{def}</td>
                    <td>{what}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p>
            The website has one setting, <code>VITE_API_URL</code>: the API's URL, or empty when the API serves the
            website itself.
          </p>
        </section>

        <section aria-labelledby="sh-limits">
          <h2 id="sh-limits" className="lp-h2">
            Free plans, honestly
          </h2>
          <ul className="plain-list">
            <li>
              Render's free plan sleeps after about 15 idle minutes; the next visit takes up to a minute to wake it,
              and the website says so while it waits.
            </li>
            <li>
              Its disk is temporary: cloned repositories and question history disappear on each restart or deploy. A
              paid instance with a disk at <code>/data</code> keeps them (uncomment <code>disk:</code> in{" "}
              <code>render.yaml</code>).
            </li>
            <li>
              With 512 MB of memory, keep <code>ARCH_EMBEDDER=none</code> and a modest <code>ARCH_MAX_REPO_MB</code>.
            </li>
            <li>
              Gemini's free tier takes about 20–40 seconds per call and caps requests per day. One question uses about
              4–10 calls.
            </li>
          </ul>
        </section>

        <section aria-labelledby="sh-vercel-api">
          <h2 id="sh-vercel-api" className="lp-h2">
            Why the API isn't on Vercel
          </h2>
          <p>
            A question runs for minutes, needs <code>git</code> and a clone on disk, and streams its progress to the
            page watching it. Vercel Functions stop after 300 seconds on the Hobby plan, don't keep files between
            requests, and may send the next request to a different instance. The website fits Vercel perfectly; the
            API needs a server that stays up.
          </p>
        </section>
      </div>
    </main>
  );
}
