import { Header } from "./components/Header";
import { useStatus } from "./hooks";
import { Home } from "./pages/Home";
import { NotFound } from "./pages/NotFound";
import { Repo } from "./pages/Repo";
import { Run } from "./pages/Run";
import { useRoute } from "./router";

export function App() {
  const route = useRoute();
  const status = useStatus();
  return (
    <>
      <a className="skip" href="#content">
        Skip to content
      </a>
      <Header status={status} />
      <div id="content" tabIndex={-1} className="content">
        {route.name === "home" && <Home status={status} />}
        {route.name === "repo" && <Repo key={route.id} id={route.id} />}
        {route.name === "run" && <Run key={route.id} id={route.id} status={status} />}
        {route.name === "notFound" && <NotFound />}
      </div>
    </>
  );
}
