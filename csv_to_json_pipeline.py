# csv_to_json_pipeline.py
# Two-pass LLM pipeline: classify CSV columns for privacy, then translate rows to a target JSON schema.
# Requires: requests  (pip install requests)
# Compatible with OpenAI API, Azure OpenAI, or any OpenAI-compatible local server.

# ============================================================
# region 1 - Configuration
# ============================================================
import csv, json, os, time
from datetime import datetime
import requests

# -- LLM credentials (set as environment variables before running) --
# Windows PowerShell:
#   $env:OPENAI_KEY      = "sk-..."
#   $env:OPENAI_ENDPOINT = "https://api.openai.com/v1"
#   $env:OPENAI_MODEL    = "gpt-4o"
# Linux / macOS:
#   export OPENAI_KEY="sk-..."
#   export OPENAI_ENDPOINT="https://api.openai.com/v1"
#   export OPENAI_MODEL="gpt-4o"
API_KEY  = os.environ.get("OPENAI_KEY",      "")
BASE_URL = os.environ.get("OPENAI_ENDPOINT", "")
MODEL    = os.environ.get("OPENAI_MODEL",    "")

# -- File paths -- update these before running --
CSV_PATH         = "source.csv"
SAMPLE_JSON_PATH = "sample_destination.json"
OUTPUT_PATH      = "output.jsonl"

# Error log is written beside the output file; timestamp prevents overwrites.
ERROR_PATH = os.path.join(
    os.path.dirname(OUTPUT_PATH) or ".",
    "errors_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".jsonl",
)

# -- Optional: override individual LLM classifications before Pass 2 runs --
# Set to a JSON object string to force specific fields to PUBLIC, PII, or SENSITIVE.
# Example: '{"EmployeeId":"PUBLIC","Salary":"PUBLIC"}'
# Leave as "{}" to rely entirely on the LLM classification.
FIELD_LABELS_OVERRIDE = os.environ.get("FIELD_LABELS_OVERRIDE", "{}")

# endregion

# ============================================================
# region 2 - Functions
# ============================================================

# region 2.1 - invoke_llm()
# Calls the OpenAI-compatible completions endpoint with streaming enabled.
# Retries up to MAX_RETRIES times on failure. Returns the full response as a string.
# -----------------------------------------------------------------------
def invoke_llm(base_url, api_key, model, system_prompt, user_prompt, label,
               MAX_RETRIES=3, MAX_TOKENS=2000, TIMEOUT=60):

    # rstrip("/") prevents a double-slash in the URL that causes a 404.
    url = base_url.rstrip("/") + "/chat/completions"

    # Authorization header is omitted when api_key is blank (local server with no auth).
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    # stream=True returns Server-Sent Events instead of a single JSON blob.
    # temperature=0 keeps output deterministic -- important for JSON responses.
    body = {
        "model":       model,
        "messages":    [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens":  MAX_TOKENS,
        "stream":      True,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        print("LLM call " + str(attempt) + "/" + str(MAX_RETRIES) + " [" + label + "]")
        try:
            content    = []
            line_count = 0

            with requests.post(url, headers=headers, json=body,
                               stream=True, timeout=TIMEOUT) as resp:
                resp.raise_for_status()

                # iter_lines() yields each SSE line; empty lines are separators -- skip them.
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    line = raw.decode("utf-8")
                    line_count += 1

                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0].get("delta", {})
                            if "content" in delta:
                                content.append(delta["content"])
                        except Exception:
                            pass   # malformed chunk -- skip silently

            result = "".join(content)
            print("  Stream: " + str(line_count) + " lines, " + str(len(result)) + " chars")
            return result

        except Exception as exc:
            print("  Attempt " + str(attempt) + " failed: " + str(exc))
            if attempt < MAX_RETRIES:
                time.sleep(2)

    raise RuntimeError("All retries failed for: " + label)

# endregion

# region 2.2 - classify_headers()
# Sends only the CSV column names to the LLM.
# Returns a dict mapping each column name to PII / SENSITIVE / PUBLIC.
# No row data is sent during this step.
# -----------------------------------------------------------------------
def classify_headers(column_names, base_url, api_key, model_name, target_keys=None):

    column_list = "\n".join(column_names)

    # If the destination schema keys are supplied, the LLM can classify source
    # fields that supply required destination fields as PUBLIC even when those
    # fields would otherwise look like PII in isolation.
    target_hint = ""
    if target_keys:
        target_hint = (
            " The destination schema requires these fields: "
            + ", ".join(sorted(target_keys))
            + ". If a source field is the only plausible source for a required"
            " destination field, classify it as PUBLIC so it reaches the"
            " translation step."
        )

    system_prompt = (
        "You are a data-privacy classifier. "
        "Given a list of database column names, classify each as exactly one of: "
        "PII (personally identifies an individual), "
        "SENSITIVE (financial, legal, or internal -- not public-facing), or "
        "PUBLIC (safe to expose externally with no privacy risk). "
        "Reply with only a valid JSON object mapping each column name to its "
        "classification. No explanation. No markdown. No code fences."
        + target_hint
    )

    user_prompt = "Classify these columns:\n" + column_list

    raw = invoke_llm(base_url, api_key, model_name,
                     system_prompt, user_prompt, "Pass 1 -- header classification")

    # Strip markdown fences if the LLM returned them despite the instruction.
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]

    return json.loads(raw.strip())

# endregion

# region 2.3 - get_field_label() / build_public_payload()
# get_field_label: deny-by-default lookup -- any unlisted field is treated as SENSITIVE.
# build_public_payload: returns a copy of the row containing only PUBLIC fields.
# -----------------------------------------------------------------------
def get_field_label(field_name, field_labels):
    return field_labels.get(field_name, "SENSITIVE")

def build_public_payload(row, field_labels):
    return {k: v for k, v in row.items() if get_field_label(k, field_labels) == "PUBLIC"}

# endregion

# region 2.4 - translate_row()
# Sends one row's PUBLIC fields plus the destination schema sample to the LLM.
# Returns a dict in the target schema's shape, or raises on parse / key failure.
# -----------------------------------------------------------------------
def translate_row(public_payload, sample_schema, base_url, api_key, model_name, row_number):

    source_json = json.dumps(public_payload)
    sample_json = json.dumps(sample_schema)

    system_prompt = (
        "You are a data-transformation engine. "
        "You receive a source record in JSON and a target schema example in JSON. "
        "Map the source fields to the target fields by matching names, meanings, "
        "and value formats. "
        "Return one JSON object that matches the target schema exactly. "
        "Use null for any target field you cannot infer from the source. "
        "No explanation. No markdown. No code fences. Only valid JSON."
    )

    user_prompt = (
        "Source record:\n" + source_json + "\n\n"
        "Target schema (example):\n" + sample_json
    )

    raw = invoke_llm(base_url, api_key, model_name,
                     system_prompt, user_prompt, "Pass 2 -- row " + str(row_number))

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]

    return json.loads(raw.strip())

# endregion

# endregion

# ============================================================
# region 3 - Main
# ============================================================

print("")
print("Input CSV    : " + CSV_PATH)
print("Sample JSON  : " + SAMPLE_JSON_PATH)
print("Output JSONL : " + OUTPUT_PATH)
print("Error log    : " + ERROR_PATH)
print("")

# -- Read column names from the CSV header (no row data loaded yet) --
with open(CSV_PATH, newline="", encoding="utf-8") as fh:
    col_names = csv.DictReader(fh).fieldnames

if not col_names:
    raise ValueError("CSV has no header row -- cannot classify columns.")

# -- Load target schema so the classifier knows what the destination needs --
with open(SAMPLE_JSON_PATH, encoding="utf-8") as fh:
    sample_schema = json.load(fh)
expected_keys = set(sample_schema.keys())

# -- Pass 1: classify column names --
print("Pass 1: classifying " + str(len(col_names)) + " columns...")
field_labels = classify_headers(col_names, BASE_URL, API_KEY, MODEL,
                                target_keys=expected_keys)

# Apply any manual overrides from FIELD_LABELS_OVERRIDE.
try:
    override = json.loads(FIELD_LABELS_OVERRIDE)
except json.JSONDecodeError:
    print("WARNING: FIELD_LABELS_OVERRIDE is not valid JSON -- ignoring.")
    override = {}
if override:
    field_labels.update(override)
    print("Applied " + str(len(override)) + " override(s).")

# Print the classification table.
col_w   = max(len(n) for n in col_names) + 2
label_w = 12
print("-" * (col_w + label_w + 4))
print(f"  {'Column':<{col_w}}{'Classification':<{label_w}}")
print("-" * (col_w + label_w + 4))
for col in col_names:
    print(f"  {col:<{col_w}}{get_field_label(col, field_labels):<{label_w}}")
print("-" * (col_w + label_w + 4))
print("")

# Confirm before processing any row data.
answer = input("Accept this classification and proceed? [y/n]: ").strip().lower()
if answer != "y":
    print("Exiting. Set FIELD_LABELS_OVERRIDE to adjust individual labels and re-run.")
    raise SystemExit(0)

print("")

# -- Pass 2: translate rows --
print("Pass 2: translating rows...")
print("")

count_translated = count_errors = count_no_public = 0

with open(OUTPUT_PATH, "w", encoding="utf-8") as out_fh, \
     open(ERROR_PATH,  "w", encoding="utf-8") as err_fh, \
     open(CSV_PATH, newline="", encoding="utf-8") as csv_fh:

    for row_number, row in enumerate(csv.DictReader(csv_fh), start=1):

        public_payload = build_public_payload(row, field_labels)

        if not public_payload:
            count_no_public += 1
            err_fh.write(json.dumps({"row": row_number, "error": "no_public_fields"},
                                    ensure_ascii=False) + "\n")
            continue

        try:
            translated = translate_row(public_payload, sample_schema,
                                       BASE_URL, API_KEY, MODEL, row_number)
            missing_keys = expected_keys - set(translated.keys())
            if missing_keys:
                raise ValueError("Missing keys: " + ", ".join(sorted(missing_keys)))
            out_fh.write(json.dumps(translated, ensure_ascii=False) + "\n")
            count_translated += 1

        except Exception as exc:
            err_fh.write(json.dumps({"row": row_number, "error": str(exc),
                                     "source": public_payload},
                                    ensure_ascii=False) + "\n")
            count_errors += 1

        if row_number % 50 == 0:
            print("  Processed " + str(row_number) + " rows...")

print("")
print("Done.")
print("  Translated : " + str(count_translated))
print("  Errors     : " + str(count_errors))
print("  Skipped    : " + str(count_no_public))
print("")
print("Output : " + OUTPUT_PATH)
if count_errors > 0 or count_no_public > 0:
    print("Errors : " + ERROR_PATH)

# endregion
