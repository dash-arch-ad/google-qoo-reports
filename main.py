import os
import json
import requests
import gspread
from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta
from oauth2client.service_account import ServiceAccountCredentials

GOOGLE_ADS_API_VERSION = "v23"
JST = ZoneInfo("Asia/Tokyo")

SHEET_NAME_KWD = "cv_kwd"
SHEET_NAME_ITEM = "cv_item"

CONVERSION_ACTION_FILTER = "purchase"


def main():
    print("=== Start Google Ads CV Export ===")

    config = load_secret()
    mask_sensitive_values(config)

    resolved = resolve_config(config)
    validate_config(resolved)

    since, until = get_last_month_range()
    print(f"Target date range: {since} to {until}")

    access_token = refresh_google_ads_access_token(
        client_id=resolved["google_ads"]["client_id"],
        client_secret=resolved["google_ads"]["client_secret"],
        refresh_token=resolved["google_ads"]["refresh_token"],
    )

    keyword_rows = fetch_keyword_cv_rows(
        access_token=access_token,
        google_ads_conf=resolved["google_ads"],
        since=since,
        until=until,
    )

    item_rows = fetch_item_cv_rows(
        access_token=access_token,
        google_ads_conf=resolved["google_ads"],
        since=since,
        until=until,
    )

    spreadsheet = connect_spreadsheet(
        sheet_id=resolved["sheet"]["spreadsheet_id"],
        google_creds_dict=resolved["sheet"]["google_service_account"],
    )

    write_to_sheet(
        spreadsheet=spreadsheet,
        sheet_name=SHEET_NAME_KWD,
        header=["keyword", "Conversion Action", "Conversions", "Conversion value"],
        rows=keyword_rows,
    )

    write_to_sheet(
        spreadsheet=spreadsheet,
        sheet_name=SHEET_NAME_ITEM,
        header=["Item ID", "Conversion Action", "Conversions", "Conversion value"],
        rows=item_rows,
    )

    print(f"Keyword rows written: {len(keyword_rows)}")
    print(f"Item rows written: {len(item_rows)}")
    print("=== Completed ===")


def load_secret():
    secret_env = os.environ.get("APP_SECRET_JSON")
    if not secret_env:
        raise RuntimeError("APP_SECRET_JSON is not set")

    try:
        return json.loads(secret_env)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"APP_SECRET_JSON is invalid JSON: {e}") from e


def mask_sensitive_values(config):
    google_ads_conf = config.get("google_ads", {})

    candidates = [
        google_ads_conf.get("developer_token"),
        google_ads_conf.get("client_id"),
        google_ads_conf.get("client_secret"),
        google_ads_conf.get("refresh_token"),
        google_ads_conf.get("customer_id"),
        google_ads_conf.get("login_customer_id"),
    ]

    for value in candidates:
        if value:
            print(f"::add-mask::{str(value).strip()}")


def resolve_config(config):
    google_ads_conf = config.get("google_ads", {})
    sheets_conf = config.get("sheets", {})

    spreadsheet_id = sheets_conf.get("spreadsheet_id")

    google_service_account = (
        config.get("gcp_service_account")
        or config.get("g_creds")
    )

    return {
        "google_ads": {
            "developer_token": google_ads_conf.get("developer_token"),
            "client_id": google_ads_conf.get("client_id"),
            "client_secret": google_ads_conf.get("client_secret"),
            "refresh_token": google_ads_conf.get("refresh_token"),
            "customer_id": normalize_customer_id(google_ads_conf.get("customer_id")),
            "login_customer_id": normalize_customer_id(
                google_ads_conf.get("login_customer_id")
            ),
        },
        "sheet": {
            "spreadsheet_id": spreadsheet_id,
            "google_service_account": normalize_google_service_account(
                google_service_account
            ),
        },
    }


def validate_config(resolved):
    required = {
        "google_ads.developer_token": resolved["google_ads"]["developer_token"],
        "google_ads.client_id": resolved["google_ads"]["client_id"],
        "google_ads.client_secret": resolved["google_ads"]["client_secret"],
        "google_ads.refresh_token": resolved["google_ads"]["refresh_token"],
        "google_ads.customer_id": resolved["google_ads"]["customer_id"],
        "sheet.spreadsheet_id": resolved["sheet"]["spreadsheet_id"],
        "sheet.google_service_account": resolved["sheet"]["google_service_account"],
    }

    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required config keys: {', '.join(missing)}")


def normalize_customer_id(value):
    if value is None:
        return None

    return str(value).strip().replace("-", "") or None


def normalize_google_service_account(creds):
    if not creds:
        return None

    fixed = dict(creds)
    private_key = fixed.get("private_key", "")

    if private_key:
        fixed["private_key"] = private_key.replace("\\n", "\n")

    return fixed


def get_last_month_range():
    today_jst = datetime.now(JST).date()

    this_month_start = date(today_jst.year, today_jst.month, 1)
    last_month_end = this_month_start - timedelta(days=1)
    last_month_start = date(last_month_end.year, last_month_end.month, 1)

    return last_month_start, last_month_end


def fetch_keyword_cv_rows(access_token, google_ads_conf, since, until):
    query = f"""
        SELECT
          ad_group_criterion.keyword.text,
          segments.conversion_action_name,
          metrics.conversions,
          metrics.conversions_value
        FROM keyword_view
        WHERE segments.date BETWEEN '{since:%Y-%m-%d}' AND '{until:%Y-%m-%d}'
          AND metrics.conversions > 0
          AND segments.conversion_action_name LIKE '%{CONVERSION_ACTION_FILTER}%'
        ORDER BY
          ad_group_criterion.keyword.text,
          segments.conversion_action_name
    """.strip()

    response = google_ads_search_stream(
        access_token=access_token,
        developer_token=google_ads_conf["developer_token"],
        customer_id=google_ads_conf["customer_id"],
        login_customer_id=google_ads_conf["login_customer_id"],
        query=query,
    )

    rows = []

    for item in response["results"]:
        rows.append([
            get_nested(item, "adGroupCriterion", "keyword", "text", default=""),
            get_nested(item, "segments", "conversionActionName", default=""),
            to_float(get_nested(item, "metrics", "conversions", default=0)),
            to_float(get_nested(item, "metrics", "conversionsValue", default=0)),
        ])

    return rows


def fetch_item_cv_rows(access_token, google_ads_conf, since, until):
    query = f"""
        SELECT
          segments.product_item_id,
          segments.conversion_action_name,
          metrics.conversions,
          metrics.conversions_value
        FROM shopping_performance_view
        WHERE segments.date BETWEEN '{since:%Y-%m-%d}' AND '{until:%Y-%m-%d}'
          AND metrics.conversions > 0
          AND segments.conversion_action_name LIKE '%{CONVERSION_ACTION_FILTER}%'
        ORDER BY
          segments.product_item_id,
          segments.conversion_action_name
    """.strip()

    response = google_ads_search_stream(
        access_token=access_token,
        developer_token=google_ads_conf["developer_token"],
        customer_id=google_ads_conf["customer_id"],
        login_customer_id=google_ads_conf["login_customer_id"],
        query=query,
    )

    rows = []

    for item in response["results"]:
        rows.append([
            get_nested(item, "segments", "productItemId", default=""),
            get_nested(item, "segments", "conversionActionName", default=""),
            to_float(get_nested(item, "metrics", "conversions", default=0)),
            to_float(get_nested(item, "metrics", "conversionsValue", default=0)),
        ])

    return rows


def refresh_google_ads_access_token(client_id, client_secret, refresh_token):
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=120,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(
            f"Google OAuth token refresh failed. "
            f"status={response.status_code}, body={truncate_text(response.text)}"
        ) from e

    payload = response.json()
    access_token = payload.get("access_token")

    if not access_token:
        raise RuntimeError(
            f"Google OAuth token refresh returned no access_token: {payload}"
        )

    print("Google Ads OAuth token refreshed successfully")
    return access_token


def google_ads_search_stream(
    access_token,
    developer_token,
    customer_id,
    login_customer_id,
    query,
):
    url = (
        f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}/customers/"
        f"{customer_id}/googleAds:searchStream"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Content-Type": "application/json",
    }

    if login_customer_id:
        headers["login-customer-id"] = login_customer_id

    response = requests.post(
        url,
        headers=headers,
        json={"query": query},
        timeout=120,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(
            f"Google Ads API request failed. "
            f"status={response.status_code}, body={truncate_text(response.text)}"
        ) from e

    payload = response.json()

    if not isinstance(payload, list):
        raise RuntimeError(
            f"Google Ads API unexpected response shape: "
            f"{truncate_text(json.dumps(payload, ensure_ascii=False))}"
        )

    all_rows = []

    for chunk in payload:
        all_rows.extend(chunk.get("results", []))

    return {
        "results": all_rows,
    }


def connect_spreadsheet(sheet_id, google_creds_dict):
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]

        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            google_creds_dict,
            scope,
        )

        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sheet_id)

        print("Google Sheets connected successfully")
        return spreadsheet

    except Exception as e:
        raise RuntimeError(f"Google Sheets connection error: {repr(e)}") from e


def write_to_sheet(spreadsheet, sheet_name, header, rows):
    try:
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=sheet_name,
                rows=max(len(rows) + 10, 1000),
                cols=len(header),
            )

        worksheet.clear()

        output = [header] + rows
        worksheet.update("A1", output)

        print(f"Write success: {sheet_name} ({len(rows)} rows)")

    except Exception as e:
        raise RuntimeError(f"Write error ({sheet_name}): {repr(e)}") from e


def get_nested(data, *keys, default=""):
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return default

        current = current.get(key)

        if current is None:
            return default

    return current


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def truncate_text(value, limit=800):
    value = str(value)

    if len(value) <= limit:
        return value

    return value[:limit] + "...(truncated)"


if __name__ == "__main__":
    main()
