/** Repository ids made by the server from a GitHub slug look like "<owner>__<name>-<8 hex>"
 *  (web/sources.py repo_id_for on clones/<owner>__<name>). After a free-plan server restarts
 *  its repositories are gone; the slug lets the page offer to open the same one again. */

const SLUG_ID = /^([A-Za-z0-9][A-Za-z0-9-]*)__([A-Za-z0-9_.-]+)-[0-9a-f]{8}$/;

/** "pallets__click-1a2b3c4d" -> "pallets/click"; null for ids of local clones. */
export function repoSlugFromId(id: string): string | null {
  const m = SLUG_ID.exec(id);
  return m ? `${m[1]}/${m[2]}` : null;
}
