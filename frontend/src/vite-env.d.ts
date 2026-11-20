/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Origin of the API when it is hosted apart from the website, e.g.
   *  "https://archaeologist-api.onrender.com". Unset or empty: same origin. */
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
