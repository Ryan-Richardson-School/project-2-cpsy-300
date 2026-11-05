import azure.functions as func
from azure.storage.blob import BlobServiceClient
import pandas as pd
import io
import json
import os
import time

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _load_diet_dataframe() -> pd.DataFrame:
    """Download All_Diets.csv from Azure Blob Storage and return it as a cleaned DataFrame."""
    conn_str = os.environ.get("AzureWebJobsStorage") or os.environ.get("STORAGE_CONNECTION_STRING")
    if not conn_str:
        raise RuntimeError("Storage connection string not configured in Azure environment variables.")

    blob_service = BlobServiceClient.from_connection_string(conn_str)
    blob_client = blob_service.get_blob_client(container="diets", blob="All_Diets.csv")
    blob_data = blob_client.download_blob().readall()

    df = pd.read_csv(io.BytesIO(blob_data))

    for col in ["Protein(g)", "Carbs(g)", "Fat(g)"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Diet_type", "Protein(g)", "Carbs(g)", "Fat(g)"])

    return df


@app.route(route="nutrition")
def get_nutrition_insights(req: func.HttpRequest) -> func.HttpResponse:
    start_time = time.time()
    
    try:
        # 1-4. Fetch connection string, download All_Diets.csv, load and clean it
        df = _load_diet_dataframe()

        # 5. Calculate analytics (from data_analysis.py)
        avg_macros = (
            df.groupby("Diet_type")[["Protein(g)", "Carbs(g)", "Fat(g)"]]
            .mean()
            .round(2)
            .reset_index()
        )
        
        top_protein = (
            df.sort_values("Protein(g)", ascending=False)
            .groupby("Diet_type")
            .head(5)[["Diet_type", "Recipe_name", "Cuisine_type", "Protein(g)"]]
        )

        highest_protein_diet = avg_macros.set_index("Diet_type")["Protein(g)"].idxmax()

        common_cuisines = (
            df.groupby("Diet_type")["Cuisine_type"]
            .agg(lambda x: x.value_counts().idxmax())
            .to_dict()
        )

        diet_distribution = df["Diet_type"].value_counts().to_dict()

        execution_time = round(time.time() - start_time, 3)

        # 6. Build the response payload
        payload = {
            "status": "success",
            "message": "Successfully retrieved and processed nutrition data from Azure Blob Storage.",
            "execution_time_seconds": execution_time,
            "total_recipes_processed": len(df),
            "highest_protein_diet": highest_protein_diet,
            "average_macronutrients": avg_macros.to_dict(orient="records"),
            "top_protein_recipes": top_protein.to_dict(orient="records"),
            "common_cuisines": common_cuisines,
            "diet_distribution": diet_distribution
        }

        return func.HttpResponse(
            json.dumps(payload),
            status_code=200,
            mimetype="application/json"
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({
                "status": "error",
                "error": str(e),
                "execution_time_seconds": round(time.time() - start_time, 3)
            }),
            status_code=500,
            mimetype="application/json"
        )


@app.route(route="recipes")
def get_recipes(req: func.HttpRequest) -> func.HttpResponse:
    start_time = time.time()

    try:
        # 1-4. Fetch connection string, download All_Diets.csv, load and clean it
        df = _load_diet_dataframe()

        # Diet types available across the whole dataset, independent of any filter below,
        # so the frontend can always populate a complete filter dropdown
        all_diet_types = sorted(df["Diet_type"].str.lower().unique().tolist())

        # 2. Optional filters: diet_type (exact match, case-insensitive) and search (substring match on recipe name)
        diet_type = req.params.get("diet_type")
        search = req.params.get("search")

        if diet_type and diet_type.lower() != "all":
            df = df[df["Diet_type"].str.lower() == diet_type.lower()]

        if search:
            df = df[df["Recipe_name"].str.contains(search, case=False, na=False)]

        # 3. Pagination parameters, with safe fallbacks for missing/invalid input
        try:
            page = max(1, int(req.params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1

        try:
            page_size = min(100, max(1, int(req.params.get("page_size", 10))))
        except (TypeError, ValueError):
            page_size = 10

        total_records = len(df)
        total_pages = max(1, -(-total_records // page_size))  # ceiling division
        page = min(page, total_pages)

        start_index = (page - 1) * page_size
        end_index = start_index + page_size
        page_df = df.iloc[start_index:end_index]

        recipes = page_df[["Diet_type", "Recipe_name", "Cuisine_type", "Protein(g)", "Carbs(g)", "Fat(g)"]].to_dict(orient="records")

        execution_time = round(time.time() - start_time, 3)

        payload = {
            "status": "success",
            "execution_time_seconds": execution_time,
            "total_records": total_records,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "diet_types": all_diet_types,
            "recipes": recipes
        }

        return func.HttpResponse(
            json.dumps(payload),
            status_code=200,
            mimetype="application/json"
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({
                "status": "error",
                "error": str(e),
                "execution_time_seconds": round(time.time() - start_time, 3)
            }),
            status_code=500,
            mimetype="application/json"
        )


@app.route(route="clusters")
def get_clusters(req: func.HttpRequest) -> func.HttpResponse:
    """Nutritional profile clustering (rule-based): Groups recipes into 4 distinct clusters based on caloric macronutrient dominance."""
    start_time = time.time()

    try:
        # 1. Load the cleaned DataFrame from Azure Blob Storage
        df = _load_diet_dataframe()

        # 2. Convert macronutrient grams to calories (Protein: 4 kcal/g, Carbs: 4 kcal/g, Fat: 9 kcal/g)
        protein_cal = df["Protein(g)"] * 4
        carbs_cal = df["Carbs(g)"] * 4
        fat_cal = df["Fat(g)"] * 9
        total_cal = (protein_cal + carbs_cal + fat_cal).replace(0, 1)  # avoid division by zero

        # 3. Calculate percentage of total calories contributed by each macronutrient
        p_pct = protein_cal / total_cal
        c_pct = carbs_cal / total_cal
        f_pct = fat_cal / total_cal

        # 4. Classify recipes into 4 intuitive clusters using calorie-based rules:
        #    - High-Protein: Protein contributes 40%+ of calories and dominates
        #    - High-Carb: Carbs contribute 45%+ of calories and dominate
        #    - High-Fat: Fat contributes 45%+ of calories and dominates
        #    - Balanced Nutrition: No single macronutrient dominates
        cluster = pd.Series("Balanced Nutrition", index=df.index)
        cluster[(p_pct >= 0.40) & (p_pct > c_pct) & (p_pct > f_pct)] = "High-Protein"
        cluster[(c_pct >= 0.45) & (c_pct > p_pct) & (c_pct > f_pct)] = "High-Carb"
        cluster[(f_pct >= 0.45) & (f_pct > p_pct) & (f_pct > c_pct)] = "High-Fat"
        df["Cluster"] = cluster
        df["p_pct"] = p_pct
        df["c_pct"] = c_pct
        df["f_pct"] = f_pct

        # 5. Metadata description and visual theme for each cluster
        cluster_info = {
            "High-Protein": {
                "description": "Recipes where protein contributes 40% or more of total calories. Supports muscle repair, recovery, and satiety.",
                "color": "#2563eb",
                "sample_label": "Highest in Protein"
            },
            "High-Carb": {
                "description": "Recipes where carbohydrates contribute 45% or more of total calories. Energy-rich, grain and plant-forward meals.",
                "color": "#16a34a",
                "sample_label": "Highest in Carbs"
            },
            "High-Fat": {
                "description": "Recipes where fat contributes 45% or more of total calories. Found across multiple diet types including Keto, Paleo, and Mediterranean.",
                "color": "#d97706",
                "sample_label": "Highest in Fat"
            },
            "Balanced Nutrition": {
                "description": "Recipes with no single dominant macronutrient. Everyday meals with moderate protein, carbs, and fat.",
                "color": "#7c3aed",
                "sample_label": "Most Balanced"
            }
        }

        # 6. Aggregate summary metrics for each cluster
        clusters_result = []
        for name, info in cluster_info.items():
            # Filter DataFrame (df) to keep only recipes belonging to the current cluster
            grp = df[df["Cluster"] == name]
            if len(grp) == 0:
                continue

            # Select the 5 most representative recipes for each cluster
            if name == "High-Protein":
                samples = grp.sort_values("p_pct", ascending=False)["Recipe_name"].head(5).tolist()
            elif name == "High-Carb":
                samples = grp.sort_values("c_pct", ascending=False)["Recipe_name"].head(5).tolist()
            elif name == "High-Fat":
                samples = grp.sort_values("f_pct", ascending=False)["Recipe_name"].head(5).tolist()
            else:  # Balanced Nutrition: recipes closest to an even 33.3% split across all 3 macros
                balance_diff = (grp["p_pct"] - 1/3).abs() + (grp["c_pct"] - 1/3).abs() + (grp["f_pct"] - 1/3).abs()
                samples = grp.loc[balance_diff.nsmallest(5).index, "Recipe_name"].tolist()

            clusters_result.append({
                "cluster_name": name,
                "description": info["description"],
                "color": info["color"],
                "sample_label": info["sample_label"],
                "recipe_count": len(grp),
                "percentage_of_total": round((len(grp) / len(df)) * 100, 1),
                "avg_macros": {
                    "Protein(g)": round(grp["Protein(g)"].mean(), 1),
                    "Carbs(g)": round(grp["Carbs(g)"].mean(), 1),
                    "Fat(g)": round(grp["Fat(g)"].mean(), 1)
                },
                "dominant_diets": grp["Diet_type"].value_counts().head(5).to_dict(),
                "sample_recipes": samples
            })

        execution_time = round(time.time() - start_time, 3)

        payload = {
            "status": "success",
            "execution_time_seconds": execution_time,
            "total_recipes_analyzed": len(df),
            "total_clusters": len(clusters_result),
            "clusters": clusters_result
        }

        return func.HttpResponse(
            json.dumps(payload),
            status_code=200,
            mimetype="application/json"
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({
                "status": "error",
                "error": str(e),
                "execution_time_seconds": round(time.time() - start_time, 3)
            }),
            status_code=500,
            mimetype="application/json"
        )

