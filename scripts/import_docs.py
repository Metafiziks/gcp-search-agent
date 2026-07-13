#!/usr/bin/env python3
"""Import documents from GCS into Vertex AI Search data store.
Called from provision.sh as a fallback if gcloud alpha command is unavailable."""
import sys
import time

project_id, datastore_id, bucket = sys.argv[1], sys.argv[2], sys.argv[3]

from google.cloud import discoveryengine_v1beta as discoveryengine

client = discoveryengine.DocumentServiceClient()
parent = (
    f"projects/{project_id}/locations/global"
    f"/collections/default_collection/dataStores/{datastore_id}/branches/default_branch"
)

request = discoveryengine.ImportDocumentsRequest(
    parent=parent,
    gcs_source=discoveryengine.GcsSource(
        input_uris=[f"gs://{bucket}/docs/**"],
        data_schema="content",
    ),
    reconciliation_mode=discoveryengine.ImportDocumentsRequest.ReconciliationMode.FULL,
)

operation = client.import_documents(request=request)
print(f"  Import operation: {operation.operation.name}", flush=True)

print("  Waiting for import to complete...", flush=True)
for i in range(60):
    time.sleep(10)
    if operation.done():
        print("  ✓ Import complete", flush=True)
        sys.exit(0)
    print(f"  Still importing... ({(i+1)*10}s elapsed)", flush=True)

print("  ⚠ Import still running in background — check Cloud Console for status", flush=True)
