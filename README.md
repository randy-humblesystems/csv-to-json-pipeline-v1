# csv-to-json-pipeline-v1
# Privacy-Aware Data Transformation AI Pipeline

A two-pass LLM pipeline that intelligently transforms CSV data into a target JSON schema — without static field mappings.

Pass 1 classifies each source column as `PII`, `SENSITIVE`, or `PUBLIC` using an LLM call that sees only column names, never row data. Pass 2 translates each row's public fields into the destination schema shape using a second LLM call, inferring field mappings and parsing composite fields (like a single `Address` string into discrete `addressLine1`, `city`, `postalCode`, and `country` fields) automatically.

---

## Requirements

- Python 3.8+
- `requests` library (`pip install requests`)
- Access to an OpenAI-compatible API endpoint (OpenAI, Azure AI Foundry, or a local server)

---

## Setup

**1. Set environment variables**

Python:
```python
os.environ["OPENAI_KEY"]      = "sk-..."
os.environ["OPENAI_ENDPOINT"] = "https://api.openai.com/v1"
os.environ["OPENAI_MODEL"]    = "gpt-4o"
```

Linux / macOS:
```bash
export OPENAI_KEY="sk-..."
export OPENAI_ENDPOINT="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-4o"
```

**2. Set file paths**

Edit the three path variables at the top of `csv_to_json_pipeline.py`:

```python
CSV_PATH         = "source.csv"
SAMPLE_JSON_PATH = "sample_destination.json"
OUTPUT_PATH      = "output.jsonl"
```

**3. Prepare your files**

- `source.csv` — the input data; any column layout works
- `sample_destination.json` — one example row in your target schema's shape; the LLM uses the key names and value formats to infer the mapping

See `sample-sourceData.csv` and `sample-destinationData.json` in this repo for the format used in the example run below.

---

## Usage

```bash
python csv_to_json_pipeline.py
```

The script prints a classification table after Pass 1 and prompts for confirmation before reading any row data. Type `y` to proceed or `n` to exit.

To override individual classifications without modifying the script, set `FIELD_LABELS_OVERRIDE` before running:

```python
# python
os.environ["FIELD_LABELS_OVERRIDE"] = '{"Address":"PUBLIC","EmployeeId":"PUBLIC","Salary":"PUBLIC"}'
```

---

## Sample Run

```
Input CSV    : C:/temp/sample-sourceData.csv
Sample JSON  : C:/temp/sample-destinationData.json
Output JSONL : C:/temp/20261022-175628-output.jsonl
Error log    : C:/temp/errors_20261022_175640.jsonl

Pass 1: classifying 6 columns...
LLM call 1/3 [Pass 1 -- header classification]
  Stream: 15 lines, 131 chars
Applied 3 override(s).
-----------------------------
  Column       Classification
-----------------------------
  Name         PII
  EmployeeId   PUBLIC
  Salary       PUBLIC
  Department   PUBLIC
  ProjectCode  PUBLIC
  Address      PUBLIC
-----------------------------

Accept this classification and proceed? [y/n]: y

Pass 2: translating rows...

LLM call 1/3 [Pass 2 -- row 1]
  ...

Done.
  Translated : 5
  Errors     : 0
  Skipped    : 0

Output : C:/temp/20261022-175628-output.jsonl
```

The composite `Address` field (`"124 Main St, Toronto, ON, Canada M9W 1M7"`) is split automatically into the destination schema's discrete fields:

```json
{
  "customerId": "E001",
  "addressLine1": "124 Main St",
  "city": "Toronto",
  "postalCode": "M9W 1M7",
  "country": "CA",
  "balanceCad": 95000,
  "accountStatus": "Active"
}
```

---

## Output Format

Results are written as JSONL (one JSON object per line). A timestamped error log is created alongside the output file; rows that fail LLM parsing or key validation are written there rather than aborting the run.

---

## Project Structure

```
csv_to_json_pipeline.py     — main script
sample-sourceData.csv       — example input
sample-destinationData.json — example target schema
```

---

## Related

Write-up and walkthrough: https://humblesystems.online
