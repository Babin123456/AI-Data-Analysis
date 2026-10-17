import sys
from pathlib import Path

# Add backend directory dynamically to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils.auth import get_current_user
from app.db.duckdb_manager import duckdb_manager

async def mock_get_current_user():
    return {"username": "testuser", "email": "test@example.com"}

app.dependency_overrides[get_current_user] = mock_get_current_user
client = TestClient(app)


@pytest.fixture
def test_dataset():
    """Uploads a test dataset containing numeric, categorical, datetime, boolean,
    and special-character column names."""
    csv_content = (
        b'"Employee Name","Department","Salary (USD)","Hiring Date","Is Remote","Years Exp"\n'
        b'"Alice Smith","Engineering",120000.50,"2021-03-15",true,5\n'
        b'"Bob Jones","Marketing",85000.00,"2022-06-01",false,3\n'
        b'"Charlie Brown","Engineering",135000.00,"2019-11-20",true,8\n'
        b'"Diana Prince","HR",72000.00,"2023-01-10",false,2\n'
        b'"Evan Wright","Marketing",91000.00,"2020-08-05",true,6\n'
    )
    resp = client.post(
        "/api/upload",
        files=[("files", ("staff_metrics.csv", csv_content, "text/csv"))],
    )
    assert resp.status_code == 200, f"Upload failed: {resp.text}"
    data = resp.json()
    dataset_id = data["dataset_id"]
    yield dataset_id
    duckdb_manager.drop_connection(dataset_id)


def test_explore_bar_chart_with_numeric_aggregation(test_dataset):
    """Bar chart with categorical X and numeric Y aggregation (SUM)."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Department",
            "y_column": "Salary (USD)",
            "aggregation": "sum",
            "chart_type": "bar",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["chart_type"] == "bar"
    assert data["chart_spec"]["type"] == "bar"
    assert "Department" in data["columns"]
    assert "Salary (USD)" in data["columns"]
    assert len(data["rows"]) == 3  # Engineering, Marketing, HR
    assert data["chart_spec"]["x_label"] == "Department"
    assert data["chart_spec"]["y_label"] == "Salary (USD)"


def test_explore_bar_chart_without_y_column_defaults_to_count(test_dataset):
    """Bar chart without Y-axis defaults to frequency count grouped by X."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Department",
            "chart_type": "bar",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "count" in data["columns"]
    assert len(data["rows"]) == 3
    # Check counts: Engineering=2, Marketing=2, HR=1
    eng_row = next(r for r in data["rows"] if r["Department"] == "Engineering")
    assert eng_row["count"] == 2


def test_explore_scatter_plot_valid_numeric_columns(test_dataset):
    """Scatter plot with two numeric columns (Years Exp vs Salary)."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Years Exp",
            "y_column": "Salary (USD)",
            "aggregation": "none",
            "chart_type": "scatter",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["chart_spec"]["type"] == "scatter"
    assert len(data["chart_spec"]["x"]) == 5
    assert len(data["chart_spec"]["y"]) == 5
    assert data["chart_spec"]["x_label"] == "Years Exp"
    assert data["chart_spec"]["y_label"] == "Salary (USD)"


def test_explore_scatter_plot_rejects_non_numeric_axes(test_dataset):
    """Scatter plot must reject non-numeric X or Y columns with a 400 error."""
    # Non-numeric X
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Department",
            "y_column": "Salary (USD)",
            "chart_type": "scatter",
        },
    )
    assert resp.status_code == 400
    assert "Scatter plot requires a numeric X-axis column" in resp.json()["detail"]

    # Non-numeric Y
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Years Exp",
            "y_column": "Employee Name",
            "chart_type": "scatter",
        },
    )
    assert resp.status_code == 400
    assert "Scatter plot requires a numeric Y-axis column" in resp.json()["detail"]


def test_explore_histogram_valid_numeric_x(test_dataset):
    """Histogram correctly runs on numeric X column without forcing Y column."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Salary (USD)",
            "chart_type": "histogram",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["chart_spec"]["type"] == "histogram"
    assert len(data["chart_spec"]["x"]) == 5
    assert data["chart_spec"]["x_label"] == "Salary (USD)"
    assert data["chart_spec"]["y_label"] == "Frequency"


def test_explore_histogram_rejects_non_numeric_x(test_dataset):
    """Histogram must reject text/categorical X-axis column."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Department",
            "chart_type": "histogram",
        },
    )
    assert resp.status_code == 400
    assert "Histogram requires a numeric X-axis column" in resp.json()["detail"]


def test_explore_box_plot(test_dataset):
    """Box plot supports numeric metric grouped by categorical X."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Department",
            "y_column": "Salary (USD)",
            "chart_type": "box",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["chart_spec"]["type"] == "box"
    assert "x" in data["chart_spec"]
    assert "y" in data["chart_spec"]
    assert data["chart_spec"]["y_label"] == "Salary (USD)"


def test_explore_line_chart_with_datetime(test_dataset):
    """Line chart with date X-axis and aggregated numeric Y-axis."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Hiring Date",
            "y_column": "Salary (USD)",
            "aggregation": "avg",
            "chart_type": "line",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["chart_spec"]["type"] == "line"
    assert data["chart_spec"]["x_label"] == "Hiring Date"
    assert data["chart_spec"]["y_label"] == "Salary (USD)"


def test_explore_pie_chart(test_dataset):
    """Pie chart with category slices."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Department",
            "chart_type": "pie",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["chart_spec"]["type"] == "pie"


def test_explore_rejects_incompatible_aggregation(test_dataset):
    """SUM/AVG on text columns must be rejected with informative error."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "Years Exp",
            "y_column": "Employee Name",
            "aggregation": "sum",
            "chart_type": "bar",
        },
    )
    assert resp.status_code == 400
    assert "Cannot calculate SUM on non-numeric column 'Employee Name'" in resp.json()["detail"]


def test_explore_rejects_nonexistent_column(test_dataset):
    """Querying an unknown column returns clean 400 error."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": "NonExistentColumn",
            "chart_type": "bar",
        },
    )
    assert resp.status_code == 400
    assert "does not exist in table" in resp.json()["detail"]


def test_explore_rejects_nonexistent_dataset():
    """Querying non-existent or expired dataset returns 404."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": "non_existent_dataset_123",
            "x_column": "col1",
            "chart_type": "bar",
        },
    )
    assert resp.status_code == 404


def test_explore_sql_injection_prevention(test_dataset):
    """SQL injection through column name is safely rejected or escaped."""
    resp = client.post(
        "/api/explore",
        json={
            "dataset_id": test_dataset,
            "x_column": 'Department"; DROP TABLE staff_metrics; --',
            "chart_type": "bar",
        },
    )
    # The column name does not exist, so it must be cleanly rejected with 400
    assert resp.status_code == 400
    assert "does not exist in table" in resp.json()["detail"]

    # Verify the table was NOT dropped!
    conn = duckdb_manager.get_connection(test_dataset)
    count = conn.execute("SELECT COUNT(*) FROM staff_metrics").fetchone()[0]
    assert count == 5
