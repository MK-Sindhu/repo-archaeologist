import { useDocumentTitle } from "../hooks";
import { Link, paths } from "../router";

export function NotFound({ what = "page" }: { what?: string }) {
  useDocumentTitle("Not found");
  return (
    <main className="container notfound">
      <h1 className="display notfound__title">Nothing at this depth.</h1>
      <p className="muted">There is no {what} at this address. It may have been removed from the list.</p>
      <p>
        <Link to={paths.home()} className="btn btn-secondary">
          Go to your repositories
        </Link>
      </p>
    </main>
  );
}
