/** Plain-language names and explanations of the agent's steps, for the public pages. */

import type { NodeName, StepEvent } from "../../api/types";

const SOURCE: Record<string, string> = {
  code: "code",
  docs: "docs",
  commits: "commit messages",
  prs: "pull requests",
};

export function stepName(ev: StepEvent): string {
  switch (ev.node) {
    case "analyze":
      return "Plan";
    case "retrieve":
      return `Search ${SOURCE[ev.source ?? ""] ?? ev.source ?? ""}`.trim();
    case "trace_history":
      return "Trace history";
    case "grade":
      return "Grade evidence";
    case "follow":
      return ev.hops ? `Follow links · hop ${ev.hops}` : "Follow links";
    case "synthesize":
      return "Draft the answer";
    case "verify":
      return "Check every claim";
    case "finalize":
      return "Answer";
  }
}

export interface StepExplainer {
  title: string;
  what: string; // what the step does, in general
}

export const EXPLAIN: Record<NodeName, StepExplainer> = {
  analyze: {
    title: "Plan",
    what:
      "The model reads the question, names the files and functions it is about, decides whether it asks why the code is this way or how it works now, and writes one search per source.",
  },
  retrieve: {
    title: "Search, four ways at once",
    what:
      "Code, docs, commit messages and pull requests are searched in parallel. Code, docs and commits come from a local index of the clone (keyword search, plus meaning-based search where the server has the memory for it); pull requests come from GitHub.",
  },
  trace_history: {
    title: "Trace the history",
    what:
      "For the code in question it follows the function itself through git log -L, so commits that only touched other parts of the file don't count.",
  },
  grade: {
    title: "Grade the evidence",
    what:
      "Every find gets a relevance grade from 0 to 3. Low grades are set aside, not deleted. The grader also picks the links worth following: the pull request a commit names, the issue a pull request fixes.",
  },
  follow: {
    title: "Follow the links",
    what:
      "Each hop fetches the linked commits, pull requests and issues in full, discussion included, and grades them too. A pull request that was closed without merging is marked as not shipped, and the answer is told not to present it as the reason.",
  },
  synthesize: {
    title: "Write the answer",
    what:
      "The answer cites a find for every claim. Why-questions separate what changed from why it changed; what and how questions explain the code as it is now.",
  },
  verify: {
    title: "Check every claim",
    what:
      "A second pass splits the draft into claims and checks each against the evidence it cites. Unsupported claims trigger one more search, then are flagged if they still can't be backed.",
  },
  finalize: {
    title: "Answer",
    what:
      "Citations that point at nothing retrieved are removed and reported. What remains is the answer, its finds, and how it was found.",
  },
};
