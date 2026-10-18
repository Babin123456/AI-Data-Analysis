import React, { useState, useEffect, useRef } from "react";
import { askQuestion } from "../../services/api.js";
import { useDataset } from "../../context/DatasetContext.jsx";
import DatasetSelector from "../shared/DatasetSelector.jsx";
import ResultTable from "../charts/ResultTable.jsx";
import SqlViewer from "../charts/SqlViewer.jsx";
import { exportChatAsHtml } from "../../utils/exportChat.js";
import { ChatAiSkeleton } from "../shared/CardSkeleton.jsx";
import { useNetworkStatus } from "../../hooks/useNetworkStatus.js";
import { toast } from "react-toastify";
import {
  AlertTriangle,
  Download,
  Trash2,
  Send,
  MessageSquare,
  Copy,
  Check,
  HelpCircle,
  Sparkles,
  Info,
} from "lucide-react";

const EXAMPLE_QUESTIONS = [
  "Which category has the highest total?",
  "Show the trend over time.",
  "What is the average value?",
  "Top 10 records.",
  "What columns are in this dataset?",
];

function generateConversationId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `conv_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
}

export default function ChatPanel() {
  const { dataset, activeFile, chatMessages, setChatMessages, clearChat: baseClearChat } = useDataset();
  const { isSlowNetwork } = useNetworkStatus();
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [popup, setPopup] = useState(null); // off-topic popup message
  const [copiedIndex, setCopiedIndex] = useState(null);
  const [conversationId, setConversationId] = useState(generateConversationId);
  const bottomRef = useRef(null);

  const clearChat = () => {
    baseClearChat();
    setConversationId(generateConversationId());
  };

  const handleCopyQuery = async (text, index) => {
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      setCopiedIndex(index);
      toast.success("Query copied to clipboard!", { autoClose: 1500, toastId: "copied-query" });
      setTimeout(() => setCopiedIndex(null), 2000);
    } catch {
      toast.error("Failed to copy query to clipboard");
    }
  };

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatMessages, isLoading]);

  // Auto-dismiss popup after 4 seconds
  useEffect(() => {
    if (!popup) return;
    const t = setTimeout(() => setPopup(null), 4000);
    return () => clearTimeout(t);
  }, [popup]);

  // Show toast when no dataset
  useEffect(() => {
    if (!dataset) {
      toast.warning("Please upload a valid file", { toastId: "no-dataset-chat" });
    }
  }, [dataset]);

  const send = async (question) => {
    if (isLoading) return;
    const raw = question ?? input;
    const q = raw.trim();
    if (!q || !dataset) return;

    if (q.length > 2000) {
      toast.warning("Question is too long (maximum 2,000 characters).");
      return;
    }

    const targetDatasetId = dataset.dataset_id;
    const targetTableName = activeFile?.table_name || null;

    setChatMessages((prev) => [...prev, { role: "user", content: q }]);
    setInput("");
    setIsLoading(true);

    try {
      const { data } = await askQuestion(targetDatasetId, q, conversationId, targetTableName);

      // Off-topic popup if marked off_topic without an answer
      if (data.off_topic && !data.answer) {
        setChatMessages((prev) => prev.slice(0, -1));
        const warnMsg = "I can only help with questions about your uploaded data.";
        setPopup(warnMsg);
        toast.warning(warnMsg);
        return;
      }

      setChatMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          ...data,
          response_type: data.response_type || (data.sql ? "data" : "text"),
        },
      ]);
    } catch (err) {
      const httpStatus = err?.response?.status;
      let errMsg;
      if (httpStatus === 401 || httpStatus === 403) {
        errMsg = "Your session has expired. Please log out and log in again.";
      } else if (httpStatus === 404) {
        errMsg = "Dataset session expired — please re-upload your file to continue.";
      } else if (httpStatus === 503) {
        errMsg = err?.response?.data?.detail || "AI service is temporarily unavailable. Please try again in a moment.";
      } else {
        errMsg = err?.response?.data?.detail || "Something went wrong. Please try again.";
      }
      toast.error(errMsg);
      setChatMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: errMsg,
          answer: errMsg,
          response_type: "error",
          warning: errMsg,
        },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  if (!dataset) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[60vh] text-center space-y-4 max-w-md mx-auto py-8 px-4 animate-fade-in">
        <div className="w-16 h-16 rounded-2xl bg-[#0071e3]/10 dark:bg-[#0071e3]/20 flex items-center justify-center text-[#0071e3] dark:text-blue-400">
          <MessageSquare size={32} />
        </div>
        <div>
          <h3 className="font-semibold text-lg text-[#1d1d1f] dark:text-[#f5f5f7]">
            No Dataset Loaded
          </h3>
          <p className="text-sm text-[#86868b] dark:text-[#a1a1a6] mt-1.5 leading-relaxed">
            Import a dataset to start asking AI questions, generating statistical summaries, and rendering automatic charts.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-[calc(100vh-120px)] relative max-w-5xl mx-auto space-y-2.5 animate-fade-in">
      {/* Header Banner */}
      <div className="flex items-center justify-between shrink-0">
        <div>
          <h2 className="font-outfit font-bold text-xl sm:text-2xl bg-gradient-to-r from-[#262422] via-[#0071e3] to-[#06b6d4] dark:from-[#f5f5f7] dark:via-[#38bdf8] dark:to-[#06b6d4] bg-clip-text text-transparent tracking-tight">
            Ask AI Assistant
          </h2>
          <p className="text-xs text-[#86868b] dark:text-[#a1a1a6] mt-0.5 font-sans">
            Natural language analysis & DuckDB SQL powered by Groq Llama 3.3.
          </p>
        </div>

        <div className="flex items-center gap-2">
          {chatMessages.some((m) => m.role === "assistant" && m.sql) && (
            <button
              onClick={() => exportChatAsHtml(chatMessages, dataset.filename)}
              className="flex items-center gap-1.5 text-xs text-[#0071e3] hover:text-[#0077ed] transition-colors px-2.5 py-1 rounded-xl bg-[#0071e3]/10 dark:bg-[#0071e3]/20 font-semibold cursor-pointer"
              title="Download entire chat as HTML report"
            >
              <Download size={13} /> Export Report
            </button>
          )}
          {chatMessages.length > 0 && (
            <button
              onClick={clearChat}
              className="flex items-center gap-1.5 text-xs text-[#86868b] hover:text-red-500 transition-colors px-2.5 py-1 rounded-xl hover:bg-black/[0.04] dark:hover:bg-white/[0.06] font-medium cursor-pointer"
              title="Clear chat history"
            >
              <Trash2 size={13} /> Clear
            </button>
          )}
        </div>
      </div>

      {/* Off-topic popup */}
      {popup && (
        <div className="absolute top-10 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 px-4 py-2 rounded-2xl shadow-lg bg-yellow-500/10 dark:bg-yellow-500/20 backdrop-blur-xl border border-yellow-500/30 text-yellow-800 dark:text-yellow-200 text-xs font-semibold animate-fade-in">
          <AlertTriangle size={14} />
          <span>{popup}</span>
          <button
            onClick={() => setPopup(null)}
            className="ml-1 text-yellow-600 hover:text-yellow-800 dark:hover:text-yellow-300 font-bold text-sm leading-none"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      )}

      {/* Messages area */}
      <div className="chat-panel-messages flex-1 overflow-y-auto space-y-4 pr-1.5 custom-scrollbar">
        {chatMessages.length === 0 && (
          <div className="flex flex-wrap gap-2 pt-2">
            {EXAMPLE_QUESTIONS.map((q) => (
              <button
                key={q}
                onClick={() => send(q)}
                className="text-xs px-3 py-1.5 rounded-full bg-[#fcfaf5]/85 dark:bg-[#1c1c1e]/70 border border-stone-200/80 dark:border-white/[0.08] hover:border-[#0071e3] text-[#515154] dark:text-[#a1a1a6] hover:text-[#0071e3] transition-all cursor-pointer shadow-2xs"
              >
                {q}
              </button>
            ))}
          </div>
        )}

        {chatMessages.map((m, i) => (
          <div key={i} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
            {m.role === "user" ? (
              <div className="flex items-center gap-1.5 max-w-[85%] justify-end group">
                <button
                  type="button"
                  onClick={() => handleCopyQuery(m.content, i)}
                  className="p-1.5 rounded-lg text-stone-400 hover:text-stone-700 dark:text-stone-500 dark:hover:text-stone-200 hover:bg-black/[0.05] dark:hover:bg-white/[0.08] transition-all opacity-70 sm:opacity-0 sm:group-hover:opacity-100 focus:opacity-100 cursor-pointer shrink-0"
                  title={copiedIndex === i ? "Copied!" : "Copy question"}
                  aria-label="Copy query"
                >
                  {copiedIndex === i ? (
                    <Check size={14} className="text-emerald-500" />
                  ) : (
                    <Copy size={14} />
                  )}
                </button>
                <div className="chat-user-bubble bg-[#0071e3] text-white rounded-2xl rounded-tr-xs px-4 py-2 text-sm shadow-sm break-words whitespace-pre-wrap">
                  {m.content}
                </div>
              </div>
            ) : (
              <div className="chat-ai-bubble w-full max-w-[95%] space-y-2.5">
                {/* Structured bubble based on response_type */}
                <div
                  className={`rounded-2xl rounded-tl-xs p-4 shadow-2xs border ${
                    m.response_type === "error" || m.warning
                      ? "bg-amber-500/[0.08] border-amber-500/30 text-amber-900 dark:text-amber-200"
                      : m.response_type === "clarification"
                      ? "bg-blue-500/[0.06] border-blue-500/30 text-[#1d1d1f] dark:text-[#f5f5f7]"
                      : "bg-[#fcfaf5]/85 dark:bg-[#1c1c1e]/80 backdrop-blur-xl border-stone-200/80 dark:border-white/[0.08]"
                  }`}
                >
                  {/* Badge header for clarification or explanation */}
                  {m.response_type === "clarification" && (
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-blue-600 dark:text-blue-400 mb-2">
                      <HelpCircle size={14} />
                      <span>Clarification Needed</span>
                    </div>
                  )}

                  {m.intent === "dataset_explanation" && (
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-teal-600 dark:text-teal-400 mb-2">
                      <Info size={14} />
                      <span>Dataset Schema & Structure</span>
                    </div>
                  )}

                  {m.response_type === "error" && (
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-amber-600 dark:text-amber-400 mb-2">
                      <AlertTriangle size={14} />
                      <span>Notice</span>
                    </div>
                  )}

                  <div className="prose dark:prose-invert max-w-none text-sm leading-relaxed text-[#262422] dark:text-[#f5f5f7] whitespace-pre-line">
                    {m.answer || m.content}
                  </div>

                  {m.warning && typeof m.warning === "string" && m.response_type !== "error" && (
                    <p className="text-xs mt-2 text-amber-600 dark:text-amber-400 flex items-center gap-1">
                      <AlertTriangle size={12} /> {m.warning}
                    </p>
                  )}
                </div>

                {/* Render SQL only for data queries with valid SQL (never empty blocks) */}
                {m.response_type === "data" && m.sql && m.sql.trim().length > 0 && (
                  <SqlViewer sql={m.sql} />
                )}

                {/* Render Result Table only when data exists */}
                {m.response_type === "data" && m.columns?.length > 0 && m.rows?.length > 0 && (
                  <div className="max-h-64 overflow-y-auto rounded-xl">
                    <ResultTable columns={m.columns} rows={m.rows} />
                  </div>
                )}

                {/* Friendly notice if data query returned empty rows */}
                {m.response_type === "data" && m.columns?.length > 0 && (!m.rows || m.rows.length === 0) && (
                  <div className="text-xs text-stone-500 dark:text-stone-400 px-3 py-2 rounded-xl bg-black/[0.02] dark:bg-white/[0.02] border border-stone-200/50 dark:border-white/[0.05]">
                    No rows matched your query criteria.
                  </div>
                )}
              </div>
            )}
          </div>
        ))}

        {isLoading && (
          <ChatAiSkeleton
            message={
              isSlowNetwork
                ? "Low network detected • Groq Llama 3.3 processing in background…"
                : "Groq Llama 3.3 analyzing dataset & generating response…"
            }
          />
        )}

        <div ref={bottomRef} className="h-1" />
      </div>

      {/* Input form */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
        className="relative shrink-0 pt-1"
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          maxLength={2000}
          placeholder="Ask anything about your data… (e.g. Total revenue by region)"
          className="chat-input w-full rounded-full border border-stone-300/60 dark:border-white/[0.12] bg-[#fcfaf5]/85 dark:bg-[#1c1c1e]/70 backdrop-blur-2xl pl-5 pr-12 py-3 text-sm text-[#262422] dark:text-[#f5f5f7] outline-none focus:ring-2 focus:ring-[#0071e3]/40 focus:border-[#0071e3] transition-all shadow-xs"
        />
        <button
          type="submit"
          disabled={isLoading || !input.trim()}
          className="absolute right-2 top-1/2 -translate-y-1/2 p-2 rounded-full bg-[#0071e3] text-white hover:bg-[#0077ed] disabled:opacity-40 transition-all cursor-pointer"
        >
          <Send size={15} />
        </button>
      </form>
    </div>
  );
}
