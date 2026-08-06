import {useEffect, useState} from "react";
import type {CatalogApi, CatalogRecipeSummary} from "../api/types";
import {RecipeSummary} from "../components/recipe-summary";

type CatalogListApi = Pick<CatalogApi, "catalogRecipes">;

export function CatalogPage({api}: {api: CatalogListApi}) {
  const [recipes, setRecipes] = useState<CatalogRecipeSummary[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void api.catalogRecipes().then(result => { if (active) setRecipes(result.recipes); }).catch(value => { if (active) setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load recipes"); });
    return () => { active = false; };
  }, [api]);
  return <>
    <div className="page-heading"><div><h2>Recipe catalog</h2><p>Local PostgreSQL is authoritative. Recipes remain available when vonkforge.ai or Git is unavailable.</p></div><div className="actions"><a className="button" href="/catalog/import/sparkrun">Import SparkRun</a><a className="button" href="/catalog/new">Create local recipe</a></div></div>
    {error && <p role="alert">{error}</p>}{!error && recipes.length === 0 && <p role="status">No recipes yet.</p>}
    <div className="recipe-grid">{recipes.map(recipe => <RecipeSummary key={recipe.recipe_id} recipe={recipe}/>)}</div>
  </>;
}
