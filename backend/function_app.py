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

