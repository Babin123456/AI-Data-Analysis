import React, { useState, useEffect, useRef, useMemo } from "react";
import { useDataset } from "../../context/DatasetContext.jsx";
import { explore } from "../../services/api.js";
import ResultChart from "../charts/ResultChart.jsx";
import ResultTable from "../charts/ResultTable.jsx";
import SqlViewer from "../charts/SqlViewer.jsx";
import DownloadButtons from "../charts/DownloadButtons.jsx";
import { ChartSkeleton } from "../shared/CardSkeleton.jsx";
import { useNetworkStatus } from "../../hooks/useNetworkStatus.js";
import { toast } from "react-toastify";
import {
  LineChart,
  Database,
  CloudRain,
  AlertCircle,
  RefreshCw,
  Hash,
  Type,
  Calendar,
  CheckCircle2,
  Info,
} from "lucide-react";

const CHART_TYPES = [
  { id: "bar", label: "Bar Chart", desc: "Compare aggregated values across categories" },
  { id: "line", label: "Line Chart", desc: "Trend over time or ordered numeric sequence" },
  { id: "scatter", label: "Scatter Plot", desc: "Relationship between two numeric variables" },
  { id: "histogram", label: "Histogram", desc: "Frequency distribution of a numeric column" },
  { id: "box", label: "Box Plot", desc: "Statistical distribution and outlier detection" },
  { id: "pie", label: "Pie Chart", desc: "Proportions and category shares" },
  { id: "area", label: "Area Chart", desc: "Volume progression across continuous axis" },
];

const AGGREGATIONS = [
  { id: "none", label: "None (Raw Data)" },
  { id: "sum", label: "Sum (SUM)" },
  { id: "avg", label: "Average (AVG)" },
  { id: "count", label: "Count (COUNT)" },
  { id: "min", label: "Minimum (MIN)" },
  { id: "max", label: "Maximum (MAX)" },
];

export default function ExplorePanel() {
  const { dataset } = useDataset();
  const { isSlowNetwork } = useNetworkStatus();

  const [selectedFileIndex, setSelectedFileIndex] = useState(0);
  const [chartType, setChartType] = useState("bar");
  const [xColumn, setXColumn] = useState("");
  const [yColumn, setYColumn] = useState("");
  const [aggregation, setAggregation] = useState("sum");

  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Stale-request tracking to prevent race conditions during rapid changes or slow network
  const activeReqIdRef = useRef(0);

  // Show toast when no dataset
  useEffect(() => {
    if (!dataset) {
      toast.warning("Please upload a valid file", { toastId: "no-dataset-explore" });
    }
  }, [dataset]);

  const files = dataset?.files || [];
  const hasMultipleFiles = files.length > 1;
  const activeFile = files[selectedFileIndex] || files[0];
  const schema = activeFile?.schema || dataset?.schema;

  // Extract and categorize columns dynamically from active schema
  const {
    allColumns,
    numericColumns,
    categoricalColumns,
    datetimeColumns,
  } = useMemo(() => {
    const rawCols = schema?.columns || [];
    const numeric = rawCols.filter((c) => c.is_numeric);
    const datetime = rawCols.filter((c) => c.is_datetime);
    const categorical = rawCols.filter(
      (c) => c.is_categorical || (!c.is_numeric && !c.is_datetime)
    );
    return {
      allColumns: rawCols,
      numericColumns: numeric,
      categoricalColumns: categorical,
      datetimeColumns: datetime,
    };
  }, [schema]);

  // Determine column compatibility constraints based on current chartType
  const chartRules = useMemo(() => {
    switch (chartType) {
      case "scatter":
        return {
          allowedX: numericColumns,
          allowedY: numericColumns,
          yRequired: true,
          yDisabled: false,
          aggAllowed: false,
          fixedAgg: "none",
          xLabel: "X-axis (Numeric Required)",
          yLabel: "Y-axis (Numeric Required)",
          hint: "Scatter plots require numeric columns on both axes to compute coordinates.",
        };
      case "histogram":
        return {
          allowedX: numericColumns,
          allowedY: [],
          yRequired: false,
          yDisabled: true,
          aggAllowed: false,
          fixedAgg: "none",
          xLabel: "X-axis (Numeric Distribution)",
          yLabel: "Y-axis (Automatic Frequency Bins)",
          hint: "Histograms calculate frequency bins automatically for a single numeric column.",
        };
      case "box":
        return {
          allowedX: allColumns,
          allowedY: numericColumns,
          yRequired: true,
          yDisabled: false,
          aggAllowed: false,
          fixedAgg: "none",
          xLabel: "Group By / Category (Optional)",
          yLabel: "Metric Value (Numeric Required)",
          hint: "Box plots visualize distribution (median, quartiles, outliers) of a numeric metric, optionally grouped by category.",
        };
      case "pie":
        return {
          allowedX: allColumns,
          allowedY: numericColumns,
          yRequired: false,
          yDisabled: false,
          aggAllowed: true,
          fixedAgg: null,
          xLabel: "Category / Slices",
          yLabel: "Values (Optional - Defaults to Count)",
          hint: "Pie charts display proportional slices for categorical groups.",
        };
      case "line":
      case "area":
        return {
          allowedX: allColumns,
          allowedY: numericColumns,
          yRequired: true,
          yDisabled: false,
          aggAllowed: true,
          fixedAgg: null,
          xLabel: "X-axis (Date, Ordered, or Category)",
          yLabel: "Y-axis (Numeric Metric)",
          hint: "Line and area charts require a numeric Y-axis metric plotted over a continuous or ordered sequence.",
        };
      case "bar":
      default:
        return {
          allowedX: allColumns,
          allowedY: numericColumns,
          yRequired: false,
          yDisabled: false,
          aggAllowed: true,
          fixedAgg: null,
          xLabel: "X-axis (Categories or Groups)",
          yLabel: "Y-axis (Optional - Defaults to Count)",
          hint: "Bar charts compare numeric totals or frequency counts across categories.",
        };
    }
  }, [chartType, numericColumns, categoricalColumns, datetimeColumns, allColumns]);

  // Reset or repair column selections when dataset, active table, or chartType changes
  useEffect(() => {
    // Invalidate any pending in-flight queries
    activeReqIdRef.current++;
    setResult(null);
    setError(null);

    if (!schema?.columns || schema.columns.length === 0) {
      setXColumn("");
      setYColumn("");
      return;
    }

    // 1. Repair or auto-select X column
    const validXNames = chartRules.allowedX.map((c) => c.name);
    let nextX = xColumn;
    if (!validXNames.includes(xColumn)) {
      if (chartType === "scatter" || chartType === "histogram") {
        nextX = numericColumns[0]?.name || "";
      } else if (categoricalColumns.length > 0) {
        nextX = categoricalColumns[0]?.name || allColumns[0]?.name || "";
      } else {
        nextX = allColumns[0]?.name || "";
      }
      setXColumn(nextX);
    }

    // 2. Repair or auto-select Y column
    if (chartRules.yDisabled) {
      setYColumn("");
    } else {
      const validYNames = chartRules.allowedY.map((c) => c.name);
      if (chartRules.yRequired && !validYNames.includes(yColumn)) {
        // Find a numeric column that is preferably different from X (for scatter)
        const candidate =
          validYNames.find((name) => name !== nextX) || validYNames[0] || "";
        setYColumn(candidate);
      } else if (!validYNames.includes(yColumn) && yColumn !== "") {
        setYColumn("");
      }
    }

    // 3. Set aggregation
    if (!chartRules.aggAllowed) {
      setAggregation(chartRules.fixedAgg || "none");
    } else if (aggregation === "none" && chartType === "bar") {
      setAggregation("sum");
    }
  }, [dataset, selectedFileIndex, chartType, schema]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!dataset) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[60vh] text-center space-y-4 max-w-md mx-auto py-8 px-4 animate-fade-in">
        <div className="w-16 h-16 rounded-2xl bg-[#0071e3]/10 dark:bg-[#0071e3]/20 flex items-center justify-center text-[#0071e3] dark:text-blue-400">
          <LineChart size={32} />
        </div>
        <div>
          <h3 className="font-semibold text-lg text-[#1d1d1f] dark:text-[#f5f5f7]">
            No Dataset Loaded
          </h3>
          <p className="text-sm text-[#86868b] dark:text-[#a1a1a6] mt-1.5 leading-relaxed">
            Upload a CSV, Excel, or SQLite file to explore columns, build interactive charts, and compute metrics.
          </p>
        </div>
      </div>
    );
  }

  // Check if compatible columns exist for current chartType
  const hasCompatibleX = chartRules.allowedX.length > 0;
  const hasCompatibleY = !chartRules.yRequired || chartRules.allowedY.length > 0;
  const isConfigurationValid =
    xColumn &&
    (!chartRules.yRequired || yColumn) &&
    hasCompatibleX &&
    hasCompatibleY;

  const runQuery = async () => {
    if (!xColumn) {
      toast.warning("Please select an X-axis column.");
      return;
    }
    if (chartRules.yRequired && !yColumn) {
      toast.warning(`Please select a Y-axis column for ${chartType.toUpperCase()} chart.`);
      return;
    }

    const currentReqId = ++activeReqIdRef.current;
    setError(null);
    setResult(null);
    setLoading(true);

    try {
      const payload = {
        dataset_id: dataset.dataset_id,
        table_name: activeFile?.table_name,
        x_column: xColumn,
        y_column: chartRules.yDisabled ? null : (yColumn || null),
        aggregation: chartRules.aggAllowed ? aggregation : (chartRules.fixedAgg || "none"),
        chart_type: chartType,
      };

      const { data } = await explore(payload);

      // Guard against stale response if user switched dataset or triggered another query
      if (activeReqIdRef.current !== currentReqId) return;

      setResult(data);
      toast.success("Chart generated successfully!");
    } catch (err) {
      if (activeReqIdRef.current !== currentReqId) return;

      const status = err?.response?.status;
      let userMsg = "Query execution failed. Please check your column selections.";

      if (status === 404) {
        userMsg = "Dataset session expired. Please re-upload your file.";
      } else if (err?.response?.data?.detail) {
        userMsg = err.response.data.detail;
      } else if (err?.message === "Network Error") {
        userMsg = "Network error: Unable to reach the server. Please check your connection.";
      }

      setError({
        message: userMsg,
        status,
      });
      toast.error(userMsg);
    } finally {
      if (activeReqIdRef.current === currentReqId) {
        setLoading(false);
      }
    }
  };

  const showChart = result && result.chart_spec && result.chart_type !== "table";
  const showTable =
    result && result.rows?.length > 0 && (!showChart || result.chart_type === "table");

  return (
    <div className="space-y-5 max-w-5xl mx-auto animate-fade-in pb-8">
      {/* Header Banner */}
      <div>
        <h2 className="font-outfit font-bold text-xl sm:text-2xl bg-gradient-to-r from-[#262422] via-[#0071e3] to-[#06b6d4] dark:from-[#f5f5f7] dark:via-[#38bdf8] dark:to-[#06b6d4] bg-clip-text text-transparent tracking-tight">
          Explore & Visualize
        </h2>
        <p className="text-xs text-[#86868b] dark:text-[#a1a1a6] mt-0.5 font-sans">
          Dynamically analyze dataset columns, run analytical DuckDB queries, and render interactive charts.
        </p>
      </div>

      {/* Dataset / Table Selector for multi-table datasets */}
      {hasMultipleFiles && (
        <div className="flex items-center gap-3 p-3.5 rounded-2xl bg-[#fcfaf5]/85 dark:bg-[#1c1c1e]/75 border border-stone-200/80 dark:border-white/[0.08] backdrop-blur-2xl">
          <div className="flex items-center gap-2 text-xs font-bold text-[#6e6e73] dark:text-[#a1a1a6] uppercase tracking-wider shrink-0">
            <Database size={14} className="text-[#0071e3]" />
            Active Table
          </div>
          <select
            value={selectedFileIndex}
            onChange={(e) => setSelectedFileIndex(Number(e.target.value))}
            className="flex-1 rounded-xl border border-stone-300/60 dark:border-white/[0.12] bg-[#f8f5ee] dark:bg-white/[0.04] px-3.5 py-2 text-sm font-medium text-[#262422] dark:text-[#f5f5f7] outline-none focus:ring-2 focus:ring-[#0071e3]/40 focus:border-[#0071e3] transition-all cursor-pointer"
          >
            {files.map((f, idx) => (
              <option key={f.table_name} value={idx}>
                {f.filename} — {f.table_name} ({f.schema?.row_count?.toLocaleString()} rows, {f.schema?.column_count} cols)
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Chart Configuration Box */}
      <div className="p-5 sm:p-6 rounded-3xl bg-[#fcfaf5]/85 dark:bg-[#1c1c1e]/75 border border-stone-200/80 dark:border-white/[0.08] backdrop-blur-2xl shadow-sm space-y-5">
        {/* Chart Type Selector */}
        <div>
          <label className="text-xs font-semibold uppercase tracking-wider text-[#6e6e73] dark:text-[#a1a1a6] mb-2 block">
            Visualization Type
          </label>
          <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-7 gap-2">
            {CHART_TYPES.map((ct) => {
              const active = chartType === ct.id;
              return (
                <button
                  key={ct.id}
                  type="button"
                  onClick={() => setChartType(ct.id)}
                  title={ct.desc}
                  className={`px-3 py-2 rounded-xl text-xs font-semibold transition-all cursor-pointer border text-center ${
                    active
                      ? "bg-[#0071e3] text-white border-[#0071e3] shadow-[0_2px_8px_rgba(0,113,227,0.3)]"
                      : "bg-[#f8f5ee] dark:bg-white/[0.04] text-[#262422] dark:text-[#f5f5f7] border-stone-200/80 dark:border-white/[0.08] hover:border-[#0071e3]/50"
                  }`}
                >
                  {ct.label}
                </button>
              );
            })}
          </div>
          <p className="text-[11px] text-[#86868b] dark:text-[#a1a1a6] mt-2 flex items-center gap-1.5">
            <Info size={12} className="text-[#0071e3] shrink-0" />
            {chartRules.hint}
          </p>
        </div>

        {/* Warning if dataset lacks required columns for chart type */}
        {(!hasCompatibleX || !hasCompatibleY) && (
          <div className="flex items-start gap-3 p-3.5 rounded-2xl bg-amber-500/10 border border-amber-500/25 text-amber-800 dark:text-amber-200 text-xs font-medium">
            <AlertCircle size={16} className="text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold">Incompatible Dataset Columns</p>
              <p className="mt-0.5 text-amber-700 dark:text-amber-300">
                {!hasCompatibleX
                  ? `This dataset contains no numeric columns required for ${chartType.toUpperCase()}. Please select a Bar or Pie chart instead.`
                  : `This dataset contains no numeric columns for Y-axis required by ${chartType.toUpperCase()}.`}
              </p>
            </div>
          </div>
        )}

        {/* Controls Grid */}
        <div className="explore-controls-grid grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4 pt-1">
          {/* X-Axis Selector */}
          <CategorizedSelect
            label={chartRules.xLabel}
            value={xColumn}
            onChange={setXColumn}
            allowedColumns={chartRules.allowedX}
            numericColumns={numericColumns}
            categoricalColumns={categoricalColumns}
            datetimeColumns={datetimeColumns}
            placeholder="Select X-axis column…"
          />

          {/* Y-Axis Selector */}
          <CategorizedSelect
            label={chartRules.yLabel}
            value={yColumn}
            onChange={setYColumn}
            allowedColumns={chartRules.allowedY}
            numericColumns={numericColumns}
            categoricalColumns={categoricalColumns}
            datetimeColumns={datetimeColumns}
            disabled={chartRules.yDisabled}
            disabledText="(Not required for Histogram)"
            placeholder={chartRules.yRequired ? "Select Y-axis metric…" : "None (Count rows)"}
            allowClear={!chartRules.yRequired}
          />

          {/* Aggregation Selector */}
          <label className="text-xs text-[#6e6e73] dark:text-gray-400 flex flex-col gap-1">
            <span className="font-medium text-[#262422] dark:text-[#f5f5f7]">Aggregation</span>
            <select
              value={chartRules.aggAllowed ? aggregation : (chartRules.fixedAgg || "none")}
              onChange={(e) => setAggregation(e.target.value)}
              disabled={!chartRules.aggAllowed}
              className="rounded-xl border border-stone-300/60 dark:border-white/[0.12] bg-[#f8f5ee] dark:bg-white/[0.04] px-3 py-2 text-sm text-[#262422] dark:text-[#f5f5f7] outline-none focus:ring-2 focus:ring-[#0071e3]/40 focus:border-[#0071e3] transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {AGGREGATIONS.map((agg) => (
                <option key={agg.id} value={agg.id}>
                  {agg.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* Action Button Bar */}
        <div className="flex items-center justify-between pt-3 border-t border-black/[0.04] dark:border-white/[0.06]">
          <div className="text-xs text-[#86868b] dark:text-[#a1a1a6]">
            {schema?.column_count ? `${schema.column_count} columns available` : ""}
          </div>
          <button
            onClick={runQuery}
            disabled={loading || !isConfigurationValid}
            className="px-6 py-2.5 rounded-full bg-[#0071e3] hover:bg-[#0077ed] text-white text-sm font-semibold shadow-[0_4px_14px_rgba(0,113,227,0.35)] hover:shadow-[0_6px_20px_rgba(0,113,227,0.45)] active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 transition-all cursor-pointer"
          >
            {loading ? (
              <>
                <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin inline-block" />
                Generating Plot…
              </>
            ) : (
              "Generate Chart"
            )}
          </button>
        </div>
      </div>

      {/* Loading Skeleton */}
      {loading && (
        <div className="space-y-2">
          {isSlowNetwork && (
            <div className="flex items-center gap-2 px-3.5 py-2 rounded-xl bg-amber-500/10 border border-amber-500/25 text-amber-800 dark:text-amber-200 text-xs font-semibold animate-fade-in">
              <CloudRain size={14} className="text-amber-600 dark:text-amber-400 shrink-0 animate-pulse" />
              <span>Low network connection detected — running DuckDB queries in background…</span>
            </div>
          )}
          <ChartSkeleton
            title={`Generating ${chartType.toUpperCase()} Chart…`}
            message={
              yColumn
                ? `Calculating ${aggregation.toUpperCase()}(${yColumn}) grouped by ${xColumn}`
                : `Computing distribution of ${xColumn}`
            }
          />
        </div>
      )}

      {/* Structured Error State */}
      {error && !loading && (
        <div className="p-5 rounded-2xl bg-red-500/10 dark:bg-red-500/15 border border-red-500/30 text-red-900 dark:text-red-200 space-y-3 animate-fade-in">
          <div className="flex items-start gap-3">
            <AlertCircle size={20} className="text-red-600 dark:text-red-400 shrink-0 mt-0.5" />
            <div className="flex-1">
              <h4 className="font-semibold text-sm">Failed to generate chart</h4>
              <p className="text-xs text-red-800 dark:text-red-300 mt-1 leading-relaxed">
                {error.message}
              </p>
            </div>
          </div>
          <div className="flex justify-end gap-2 pt-1 border-t border-red-500/20">
            <button
              onClick={runQuery}
              className="px-4 py-1.5 rounded-xl bg-red-600 hover:bg-red-700 text-white text-xs font-semibold flex items-center gap-1.5 transition-all shadow-xs cursor-pointer"
            >
              <RefreshCw size={12} />
              Retry Query
            </button>
          </div>
        </div>
      )}

      {/* Successful Results */}
      {result && !loading && (
        <div className="space-y-4 animate-fade-in">
          {showChart && (
            <ResultChart chartType={result.chart_type} chartSpec={result.chart_spec} />
          )}

          {showTable && (
            <ResultTable columns={result.columns} rows={result.rows} />
          )}

          {!showChart && !showTable && (
            <div className="p-6 rounded-2xl border border-stone-200/80 dark:border-white/[0.08] bg-[#fcfaf5]/85 dark:bg-[#1c1c1e]/75 text-center text-sm text-[#86868b] dark:text-[#a1a1a6]">
              <Info size={20} className="mx-auto mb-2 text-[#0071e3]" />
              No data returned for this selection. Try adjusting your columns or aggregation.
            </div>
          )}

          <SqlViewer sql={result.sql} />
          <DownloadButtons datasetId={dataset.dataset_id} sql={result.sql} />
        </div>
      )}
    </div>
  );
}

/**
 * Robust dropdown with grouped <optgroup> displaying column types (Numeric, Categorical, Datetime).
 * Safely handles arbitrary column names (spaces, uppercase, symbols).
 */
function CategorizedSelect({
  label,
  value,
  onChange,
  allowedColumns,
  numericColumns,
  categoricalColumns,
  datetimeColumns,
  disabled = false,
  disabledText = "Disabled",
  placeholder = "Select column…",
  allowClear = false,
}) {
  const allowedNames = useMemo(
    () => new Set(allowedColumns.map((c) => c.name)),
    [allowedColumns]
  );

  const numGroup = useMemo(
    () => numericColumns.filter((c) => allowedNames.has(c.name)),
    [numericColumns, allowedNames]
  );
  const catGroup = useMemo(
    () => categoricalColumns.filter((c) => allowedNames.has(c.name)),
    [categoricalColumns, allowedNames]
  );
  const dateGroup = useMemo(
    () => datetimeColumns.filter((c) => allowedNames.has(c.name)),
    [datetimeColumns, allowedNames]
  );

  return (
    <label className="text-xs text-[#6e6e73] dark:text-gray-400 flex flex-col gap-1">
      <span className="font-medium text-[#262422] dark:text-[#f5f5f7]">{label}</span>
      <select
        value={disabled ? "" : value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className="rounded-xl border border-stone-300/60 dark:border-white/[0.12] bg-[#f8f5ee] dark:bg-white/[0.04] px-3 py-2 text-sm text-[#262422] dark:text-[#f5f5f7] outline-none focus:ring-2 focus:ring-[#0071e3]/40 focus:border-[#0071e3] transition-all disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
      >
        {disabled ? (
          <option value="">{disabledText}</option>
        ) : (
          <>
            <option value="">{allowClear ? "— None —" : placeholder}</option>

            {numGroup.length > 0 && (
              <optgroup label="🔢 Numeric Columns">
                {numGroup.map((col) => (
                  <option key={col.name} value={col.name}>
                    {col.name} ({col.dtype})
                  </option>
                ))}
              </optgroup>
            )}

            {catGroup.length > 0 && (
              <optgroup label="🔤 Categorical / Text Columns">
                {catGroup.map((col) => (
                  <option key={col.name} value={col.name}>
                    {col.name} ({col.dtype})
                  </option>
                ))}
              </optgroup>
            )}

            {dateGroup.length > 0 && (
              <optgroup label="📅 Date & Time Columns">
                {dateGroup.map((col) => (
                  <option key={col.name} value={col.name}>
                    {col.name} ({col.dtype})
                  </option>
                ))}
              </optgroup>
            )}
          </>
        )}
      </select>
    </label>
  );
}
