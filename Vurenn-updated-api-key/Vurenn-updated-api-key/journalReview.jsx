import { useState, useEffect, useCallback, useMemo } from "react";
import {
  FileText,
  Send,
  CheckCircle2,
  XCircle,
  Clock,
  Pencil,
  Trash2,
  ChevronDown,
  ChevronRight,
  AlertCircle,
  Plus,
  ArrowLeft,
  Inbox,
  History,
} from "lucide-react";

/* ---------------------------------------------------------------------- */
/*  API helper                                                            */
/* ---------------------------------------------------------------------- */

function useJournalApi({ apiBaseUrl, authToken }) {
  return useCallback(
    async (path, options = {}) => {
      const response = await fetch(`${apiBaseUrl}${path}`, {
        ...options,
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${authToken}`,
          ...(options.headers || {}),
        },
      });
      if (response.status === 204) return null;
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(data?.message || `Request failed (${response.status})`);
      }
      return data;
    },
    [apiBaseUrl, authToken]
  );
}

function cloneContent(value) {
  return JSON.parse(JSON.stringify(value || { intro: "", updates: [], team: [] }));
}

/* ---------------------------------------------------------------------- */
/*  Status badge                                                          */
/* ---------------------------------------------------------------------- */

const STATUS_META = {
  pending_review: { label: "Pending review", classes: "bg-amber-50 text-amber-700 border-amber-200", Icon: Clock },
  approved: { label: "Approved", classes: "bg-emerald-50 text-emerald-700 border-emerald-200", Icon: CheckCircle2 },
  rejected: { label: "Rejected", classes: "bg-rose-50 text-rose-700 border-rose-200", Icon: XCircle },
};

function StatusBadge({ status }) {
  const meta = STATUS_META[status] || STATUS_META.pending_review;
  const { Icon } = meta;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium ${meta.classes}`}
    >
      <Icon className="h-3 w-3" />
      {meta.label}
    </span>
  );
}

/* ---------------------------------------------------------------------- */
/*  Content editor (intro / updates / team)                               */
/* ---------------------------------------------------------------------- */

function LabeledInput({ label, value, onChange, maxLength, placeholder, multiline }) {
  const Comp = multiline ? "textarea" : "input";
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-500">{label}</span>
      <Comp
        value={value}
        maxLength={maxLength}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        rows={multiline ? 3 : undefined}
        className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-200"
      />
    </label>
  );
}

function UpdateRow({ item, onChange, onRemove }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/60 p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="grid flex-1 grid-cols-2 gap-3">
          <LabeledInput label="Date" value={item.date} maxLength={40} onChange={(v) => onChange({ ...item, date: v })} />
          <LabeledInput label="Category" value={item.category} maxLength={40} onChange={(v) => onChange({ ...item, category: v })} />
        </div>
        <button
          type="button"
          onClick={onRemove}
          className="mt-6 shrink-0 rounded-lg p-1.5 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
          aria-label="Remove update"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
      <div className="mb-3">
        <LabeledInput label="Title" value={item.title} maxLength={120} onChange={(v) => onChange({ ...item, title: v })} />
      </div>
      <LabeledInput
        label="Summary"
        value={item.summary}
        maxLength={500}
        multiline
        onChange={(v) => onChange({ ...item, summary: v })}
      />
    </div>
  );
}

function TeamRow({ item, onChange, onRemove }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/60 p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="grid flex-1 grid-cols-2 gap-3">
          <LabeledInput label="Name" value={item.name} maxLength={80} onChange={(v) => onChange({ ...item, name: v })} />
          <LabeledInput label="Role" value={item.role} maxLength={120} onChange={(v) => onChange({ ...item, role: v })} />
        </div>
        <button
          type="button"
          onClick={onRemove}
          className="mt-6 shrink-0 rounded-lg p-1.5 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
          aria-label="Remove team member"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
      <LabeledInput label="Note" value={item.note} maxLength={400} multiline onChange={(v) => onChange({ ...item, note: v })} />
    </div>
  );
}

function ContentEditor({ value, onChange }) {
  const content = value || { intro: "", updates: [], team: [] };

  const setField = (field, next) => onChange({ ...content, [field]: next });

  const setUpdateAt = (index, next) => {
    const updates = [...content.updates];
    updates[index] = next;
    setField("updates", updates);
  };
  const removeUpdateAt = (index) => setField("updates", content.updates.filter((_, i) => i !== index));
  const addUpdate = () =>
    setField("updates", [
      { date: "", category: "", title: "", summary: "" },
      ...content.updates,
    ]);

  const setTeamAt = (index, next) => {
    const team = [...content.team];
    team[index] = next;
    setField("team", team);
  };
  const removeTeamAt = (index) => setField("team", content.team.filter((_, i) => i !== index));
  const addTeam = () => setField("team", [...content.team, { name: "", role: "", note: "" }]);

  return (
    <div className="space-y-6">
      <LabeledInput
        label="Intro"
        value={content.intro}
        maxLength={600}
        multiline
        onChange={(v) => setField("intro", v)}
        placeholder="This is the public record of..."
      />

      <div>
        <div className="mb-2 flex items-center justify-between">
          <h4 className="text-sm font-semibold text-slate-700">Updates</h4>
          <button
            type="button"
            onClick={addUpdate}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50"
          >
            <Plus className="h-3.5 w-3.5" /> Add update
          </button>
        </div>
        <div className="space-y-3">
          {content.updates.map((item, index) => (
            <UpdateRow
              // eslint-disable-next-line react/no-array-index-key
              key={index}
              item={item}
              onChange={(next) => setUpdateAt(index, next)}
              onRemove={() => removeUpdateAt(index)}
            />
          ))}
          {content.updates.length === 0 && (
            <p className="rounded-lg border border-dashed border-slate-200 px-3 py-4 text-center text-xs text-slate-400">
              No updates yet.
            </p>
          )}
        </div>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <h4 className="text-sm font-semibold text-slate-700">Team</h4>
          <button
            type="button"
            onClick={addTeam}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50"
          >
            <Plus className="h-3.5 w-3.5" /> Add person
          </button>
        </div>
        <div className="space-y-3">
          {content.team.map((item, index) => (
            <TeamRow
              // eslint-disable-next-line react/no-array-index-key
              key={index}
              item={item}
              onChange={(next) => setTeamAt(index, next)}
              onRemove={() => removeTeamAt(index)}
            />
          ))}
          {content.team.length === 0 && (
            <p className="rounded-lg border border-dashed border-slate-200 px-3 py-4 text-center text-xs text-slate-400">
              No team entries yet.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/*  Diff view                                                             */
/* ---------------------------------------------------------------------- */

function diffRows(publishedRows, draftRows, keyField) {
  const publishedByKey = new Map(publishedRows.map((row) => [row[keyField], row]));
  const draftByKey = new Map(draftRows.map((row) => [row[keyField], row]));
  const rows = [];

  draftRows.forEach((row) => {
    const key = row[keyField];
    const before = publishedByKey.get(key);
    if (!before) {
      rows.push({ type: "added", after: row });
    } else if (JSON.stringify(before) !== JSON.stringify(row)) {
      rows.push({ type: "changed", before, after: row });
    } else {
      rows.push({ type: "unchanged", after: row });
    }
  });
  publishedRows.forEach((row) => {
    const key = row[keyField];
    if (!draftByKey.has(key)) {
      rows.push({ type: "removed", before: row });
    }
  });
  return rows;
}

const DIFF_STYLES = {
  added: "border-emerald-200 bg-emerald-50/70",
  removed: "border-rose-200 bg-rose-50/70 line-through decoration-rose-300",
  changed: "border-amber-200 bg-amber-50/70",
  unchanged: "border-slate-200 bg-white",
};

const DIFF_LABEL = { added: "Added", removed: "Removed", changed: "Changed", unchanged: null };

function DraftDiff({ published, draft }) {
  const introChanged = (published?.intro || "") !== (draft?.intro || "");
  const updateDiffs = useMemo(
    () => diffRows(published?.updates || [], draft?.updates || [], "title"),
    [published, draft]
  );
  const teamDiffs = useMemo(
    () => diffRows(published?.team || [], draft?.team || [], "name"),
    [published, draft]
  );

  return (
    <div className="space-y-5">
      <div>
        <h4 className="mb-1.5 text-sm font-semibold text-slate-700">Intro</h4>
        <p
          className={`rounded-lg border px-3 py-2 text-sm text-slate-700 ${
            introChanged ? DIFF_STYLES.changed : DIFF_STYLES.unchanged
          }`}
        >
          {draft?.intro || <span className="text-slate-400">Empty</span>}
        </p>
      </div>

      <div>
        <h4 className="mb-1.5 text-sm font-semibold text-slate-700">Updates</h4>
        <div className="space-y-2">
          {updateDiffs.map((diff, index) => {
            const row = diff.after || diff.before;
            return (
              // eslint-disable-next-line react/no-array-index-key
              <div key={index} className={`rounded-lg border px-3 py-2 text-sm ${DIFF_STYLES[diff.type]}`}>
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-slate-800">{row.title || "Untitled update"}</span>
                  {DIFF_LABEL[diff.type] && (
                    <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                      {DIFF_LABEL[diff.type]}
                    </span>
                  )}
                </div>
                <p className="mt-0.5 text-xs text-slate-500">
                  {row.date} · {row.category}
                </p>
              </div>
            );
          })}
          {updateDiffs.length === 0 && <p className="text-xs text-slate-400">No updates.</p>}
        </div>
      </div>

      <div>
        <h4 className="mb-1.5 text-sm font-semibold text-slate-700">Team</h4>
        <div className="space-y-2">
          {teamDiffs.map((diff, index) => {
            const row = diff.after || diff.before;
            return (
              // eslint-disable-next-line react/no-array-index-key
              <div key={index} className={`rounded-lg border px-3 py-2 text-sm ${DIFF_STYLES[diff.type]}`}>
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-slate-800">{row.name || "Unnamed"}</span>
                  {DIFF_LABEL[diff.type] && (
                    <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                      {DIFF_LABEL[diff.type]}
                    </span>
                  )}
                </div>
                <p className="mt-0.5 text-xs text-slate-500">{row.role}</p>
              </div>
            );
          })}
          {teamDiffs.length === 0 && <p className="text-xs text-slate-400">No team entries.</p>}
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/*  Draft list row                                                        */
/* ---------------------------------------------------------------------- */

function DraftListItem({ draft, expanded, onToggle, published, actions }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <div className="flex min-w-0 items-center gap-2">
          {expanded ? (
            <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" />
          ) : (
            <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" />
          )}
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-slate-800">
              {draft.note || "Journal update"}
            </p>
            <p className="truncate text-xs text-slate-500">
              {draft.submitted_by} · {new Date(draft.submitted_at).toLocaleString()}
            </p>
          </div>
        </div>
        <StatusBadge status={draft.status} />
      </button>
      {expanded && (
        <div className="border-t border-slate-100 px-4 py-4">
          <DraftDiff published={published} draft={draft.content} />
          {draft.status !== "pending_review" && (
            <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
              Reviewed by {draft.reviewed_by} on {new Date(draft.reviewed_at).toLocaleString()}
              {draft.review_note ? ` — "${draft.review_note}"` : ""}
            </div>
          )}
          {actions && <div className="mt-4 flex flex-wrap gap-2">{actions}</div>}
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/*  Main component                                                        */
/* ---------------------------------------------------------------------- */

export default function JournalReviewSheet({
  apiBaseUrl,
  authToken,
  isAdmin = false,
  currentUserEmail = "",
}) {
  const request = useJournalApi({ apiBaseUrl, authToken });

  const [published, setPublished] = useState(null);
  const [drafts, setDrafts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [view, setView] = useState(isAdmin ? "queue" : "mine");
  const [composer, setComposer] = useState(null);
  const [composerNote, setComposerNote] = useState("");
  const [editingDraftId, setEditingDraftId] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  const [reviewModal, setReviewModal] = useState(null);
  const [reviewNote, setReviewNote] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [journal, draftResponse] = await Promise.all([
        request("/v1/public/journal"),
        request(isAdmin ? "/v1/admin/journal/drafts" : "/v1/team/journal/drafts"),
      ]);
      setPublished(journal);
      setDrafts(draftResponse?.items || []);
    } catch (err) {
      setError(err.message || "Could not load the review sheet.");
    } finally {
      setLoading(false);
    }
  }, [request, isAdmin]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const pendingDrafts = drafts.filter((draft) => draft.status === "pending_review");
  const reviewedDrafts = drafts.filter((draft) => draft.status !== "pending_review");
  const myDrafts = isAdmin ? drafts.filter((draft) => draft.submitted_by === currentUserEmail) : drafts;

  function startNewDraft() {
    setComposer(cloneContent(published));
    setComposerNote("");
    setEditingDraftId(null);
    setView("new");
  }

  function startEditDraft(draft) {
    setComposer(cloneContent(draft.content));
    setComposerNote(draft.note || "");
    setEditingDraftId(draft.id);
    setView("new");
  }

  async function submitComposer() {
    setSubmitting(true);
    setError("");
    try {
      if (editingDraftId) {
        await request(`/v1/team/journal/drafts/${editingDraftId}`, {
          method: "PATCH",
          body: JSON.stringify({ content: composer, note: composerNote }),
        });
      } else {
        await request("/v1/team/journal/drafts", {
          method: "POST",
          body: JSON.stringify({ content: composer, note: composerNote }),
        });
      }
      setView("mine");
      await loadAll();
    } catch (err) {
      setError(err.message || "Could not submit the draft.");
    } finally {
      setSubmitting(false);
    }
  }

  async function withdrawDraft(draftId) {
    setSubmitting(true);
    setError("");
    try {
      await request(`/v1/team/journal/drafts/${draftId}`, { method: "DELETE" });
      await loadAll();
    } catch (err) {
      setError(err.message || "Could not withdraw the draft.");
    } finally {
      setSubmitting(false);
    }
  }

  async function submitReview() {
    if (!reviewModal) return;
    if (reviewModal.action === "reject" && !reviewNote.trim()) {
      setError("Explain why this draft was not approved.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await request(`/v1/admin/journal/drafts/${reviewModal.draftId}/${reviewModal.action}`, {
        method: "PUT",
        body: JSON.stringify({ review_note: reviewNote }),
      });
      setReviewModal(null);
      setReviewNote("");
      await loadAll();
    } catch (err) {
      setError(err.message || "Could not submit the review.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl">
      <div className="mb-5 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-slate-900 text-white">
            <FileText className="h-4.5 w-4.5" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-slate-900">Journal review sheet</h2>
            <p className="text-xs text-slate-500">Drafts are reviewed before they go live.</p>
          </div>
        </div>
        {view !== "new" && (
          <button
            type="button"
            onClick={startNewDraft}
            className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800"
          >
            <Plus className="h-4 w-4" /> New draft
          </button>
        )}
      </div>

      {error && (
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {isAdmin && view !== "new" && (
        <div className="mb-4 flex gap-1 rounded-lg border border-slate-200 bg-slate-50 p-1">
          <button
            type="button"
            onClick={() => setView("queue")}
            className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium ${
              view === "queue" ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"
            }`}
          >
            <Inbox className="h-3.5 w-3.5" /> Review queue
            {pendingDrafts.length > 0 && (
              <span className="rounded-full bg-amber-100 px-1.5 text-xs font-semibold text-amber-700">
                {pendingDrafts.length}
              </span>
            )}
          </button>
          <button
            type="button"
            onClick={() => setView("history")}
            className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium ${
              view === "history" ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"
            }`}
          >
            <History className="h-3.5 w-3.5" /> History
          </button>
        </div>
      )}

      {loading ? (
        <div className="py-16 text-center text-sm text-slate-400">Loading…</div>
      ) : view === "new" ? (
        <div>
          <button
            type="button"
            onClick={() => setView(isAdmin ? "queue" : "mine")}
            className="mb-4 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700"
          >
            <ArrowLeft className="h-3.5 w-3.5" /> Back
          </button>
          <div className="mb-4">
            <LabeledInput
              label="Note to reviewer (optional)"
              value={composerNote}
              maxLength={400}
              placeholder="What changed and why"
              onChange={setComposerNote}
            />
          </div>
          <ContentEditor value={composer} onChange={setComposer} />
          <div className="mt-6 flex justify-end gap-2 border-t border-slate-100 pt-4">
            <button
              type="button"
              onClick={() => setView(isAdmin ? "queue" : "mine")}
              className="rounded-lg border border-slate-200 px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={submitting}
              onClick={submitComposer}
              className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
            >
              <Send className="h-3.5 w-3.5" />
              {submitting ? "Submitting…" : editingDraftId ? "Resubmit draft" : "Submit for review"}
            </button>
          </div>
        </div>
      ) : isAdmin && view === "queue" ? (
        <div className="space-y-3">
          {pendingDrafts.length === 0 && (
            <p className="rounded-xl border border-dashed border-slate-200 px-4 py-10 text-center text-sm text-slate-400">
              Nothing waiting for review.
            </p>
          )}
          {pendingDrafts.map((draft) => (
            <DraftListItem
              key={draft.id}
              draft={draft}
              published={published}
              expanded={expandedId === draft.id}
              onToggle={() => setExpandedId(expandedId === draft.id ? null : draft.id)}
              actions={
                <>
                  <button
                    type="button"
                    onClick={() => setReviewModal({ draftId: draft.id, action: "approve" })}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700"
                  >
                    <CheckCircle2 className="h-3.5 w-3.5" /> Approve &amp; publish
                  </button>
                  <button
                    type="button"
                    onClick={() => setReviewModal({ draftId: draft.id, action: "reject" })}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 px-3 py-1.5 text-sm font-medium text-rose-600 hover:bg-rose-50"
                  >
                    <XCircle className="h-3.5 w-3.5" /> Reject
                  </button>
                </>
              }
            />
          ))}
        </div>
      ) : isAdmin && view === "history" ? (
        <div className="space-y-3">
          {reviewedDrafts.length === 0 && (
            <p className="rounded-xl border border-dashed border-slate-200 px-4 py-10 text-center text-sm text-slate-400">
              No reviewed drafts yet.
            </p>
          )}
          {reviewedDrafts.map((draft) => (
            <DraftListItem
              key={draft.id}
              draft={draft}
              published={published}
              expanded={expandedId === draft.id}
              onToggle={() => setExpandedId(expandedId === draft.id ? null : draft.id)}
            />
          ))}
        </div>
      ) : (
        <div className="space-y-3">
          {myDrafts.length === 0 && (
            <p className="rounded-xl border border-dashed border-slate-200 px-4 py-10 text-center text-sm text-slate-400">
              You haven't submitted any journal drafts yet.
            </p>
          )}
          {myDrafts.map((draft) => (
            <DraftListItem
              key={draft.id}
              draft={draft}
              published={published}
              expanded={expandedId === draft.id}
              onToggle={() => setExpandedId(expandedId === draft.id ? null : draft.id)}
              actions={
                draft.status === "pending_review" && (
                  <>
                    <button
                      type="button"
                      onClick={() => startEditDraft(draft)}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50"
                    >
                      <Pencil className="h-3.5 w-3.5" /> Edit
                    </button>
                    <button
                      type="button"
                      disabled={submitting}
                      onClick={() => withdrawDraft(draft.id)}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 px-3 py-1.5 text-sm font-medium text-rose-600 hover:bg-rose-50 disabled:opacity-50"
                    >
                      <Trash2 className="h-3.5 w-3.5" /> Withdraw
                    </button>
                  </>
                )
              }
            />
          ))}
        </div>
      )}

      {reviewModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 px-4">
          <div className="w-full max-w-sm rounded-xl bg-white p-5 shadow-xl">
            <h3 className="mb-1 text-sm font-semibold text-slate-900">
              {reviewModal.action === "approve" ? "Approve and publish this draft?" : "Reject this draft"}
            </h3>
            <p className="mb-3 text-xs text-slate-500">
              {reviewModal.action === "approve"
                ? "This immediately replaces the live journal content."
                : "The author will see this note and can revise and resubmit."}
            </p>
            <LabeledInput
              label={reviewModal.action === "reject" ? "Reason (required)" : "Note (optional)"}
              value={reviewNote}
              maxLength={400}
              multiline
              onChange={setReviewNote}
            />
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  setReviewModal(null);
                  setReviewNote("");
                }}
                className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={submitting}
                onClick={submitReview}
                className={`rounded-lg px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50 ${
                  reviewModal.action === "approve"
                    ? "bg-emerald-600 hover:bg-emerald-700"
                    : "bg-rose-600 hover:bg-rose-700"
                }`}
              >
                {submitting ? "Working…" : reviewModal.action === "approve" ? "Approve & publish" : "Reject"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
