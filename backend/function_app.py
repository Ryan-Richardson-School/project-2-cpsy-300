import azure.functions as func
from azure.storage.blob import BlobServiceClient
import pandas as pd
import io
import json
import os
import time

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="nutrition")
def get_nutrition_insights(req: func.HttpRequest) -> func.HttpResponse:
    start_time = time.time()
    
    try:
        # 1. Fetch connection string from Azure environment settings
        conn_str = os.environ.get("AzureWebJobsStorage") or os.environ.get("STORAGE_CONNECTION_STRING")
        if not conn_str:
            return func.HttpResponse(
                json.dumps({"error": "Storage connection string not configured in Azure environment variables."}),
                status_code=500,
                mimetype="application/json"
            )

        # 2. Connect to live Azure Storage and download All_Diets.csv from the "diets" container
        blob_service = BlobServiceClient.from_connection_string(conn_str)
        blob_client = blob_service.get_blob_client(container="diets", blob="All_Diets.csv")
        blob_data = blob_client.download_blob().readall()

        # 3. Load into pandas DataFrame
        df = pd.read_csv(io.BytesIO(blob_data))

        # 4. Clean numeric columns
        for col in ["Protein(g)", "Carbs(g)", "Fat(g)"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["Diet_type", "Protein(g)", "Carbs(g)", "Fat(g)"])

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
            .head(5)[["Diet_type", "Recipe_name", "Protein(g)"]]
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

