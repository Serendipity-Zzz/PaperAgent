import { lazy, Suspense, useEffect, useRef, useState, type CSSProperties } from "react";
import { create } from "zustand";

import { usePanePreferences } from "./features/layout/usePanePreferences";
import {
  DEFAULT_PREVIEW_VIEW_STATE,
  previewTabKey,
  upsertPreviewTab,
  type PreviewTabState,
  type PreviewViewState,
} from "./features/preview/tabStore";
import {
  boundedMessageWindow,
  LATEST_MESSAGE_CURSOR,
  MESSAGE_WINDOW_SIZE,
} from "./features/workspace/messageWindow";
import {
  ActiveRunCard,
  HistoricalRunTrace,
} from "./features/workspace/RunTraceCard";
import {
  mergePublicRunEvents,
  publicFailureMessage,
  TERMINAL_RUN_STATUSES,
} from "./features/workspace/runTraceModel";
import type { PreviewArtifact, PreviewPart } from "./PreviewPane";

const MarkdownContent = lazy(async () => {
  const module = await import("./shared/markdown/MarkdownContent");
  return { default: module.MarkdownContent };
});

const PreviewPane = lazy(async () => {
  const module = await import("./PreviewPane");
  return { default: module.PreviewPane };
});

type WorkspaceState = {
  previewOpen: boolean;
  drafts: Record<string, string>;
  togglePreview: () => void;
  saveDraft: (sessionId: string, value: string) => void;
};

const useWorkspace = create<WorkspaceState>((set) => ({
  previewOpen: false,
  drafts: {},
  togglePreview: () => set((state) => ({ previewOpen: !state.previewOpen })),
  saveDraft: (sessionId, value) =>
    set((state) => ({ drafts: { ...state.drafts, [sessionId]: value } })),
}));

type CreatedProject = { id: string; name: string; status?: string; archived?: boolean };
type CreatedSession = { id: string; title: string; status?: string; draft?: string; last_read_sequence?: number };
type ChatMessageView = {
  id: string;
  session_id: string;
  role: string;
  content: string;
  created_at: string;
  sequence?: number;
  status?: string;
  run_id?: string | null;
  artifact_links?: ArtifactLinkView[];
};
type AgentTurnResult = {
  message: ChatMessageView;
  task_id: string;
  rounds: number;
  tool_call_count: number;
  routes: string[];
  prompt: { hash: string; modules: string[]; runtime_snapshot_id: string };
};
type AgentTask = {
  id: string;
  kind: string;
  status: "pending" | "running" | "waiting_approval" | "paused" | "completed" | "failed" | "cancelled" | "superseded";
  conversation_id?: string | null;
  current_phase?: string;
  attempt?: number;
  version?: number;
  started_at?: string | null;
  first_output_at?: string | null;
  finished_at?: string | null;
  updated_at?: string;
  recovery_strategy?: string | null;
  unread?: boolean;
  payload: {
    session_id: string;
    content: string;
    provider_id?: string | null;
    result?: AgentTurnResult;
    error?: string;
    recovery_required?: boolean;
    pending_guidance?: Array<Record<string, unknown>>;
    replan_required?: boolean;
    recovery_options?: Array<{
      id: string;
      kind: "revision" | "asset";
      title: string;
      subtitle?: string;
      thumbnail_url?: string;
      source_run_id?: string;
      hash?: string;
    }>;
    failure_category?: string;
    failure_details?: Record<string, unknown>;
  };
};
type SteeringEnvelopeView = {
  decision_id: string;
  target_run_id: string;
  response_mode: string;
  relationship: string;
  impact_level: "L0" | "L1" | "L2" | "L3" | "L4" | "L5";
  action_on_a: string;
  affected_nodes: string[];
  preserved_nodes: string[];
  earliest_affected_checkpoint?: string | null;
  confidence: number;
  confirmation_required: boolean;
  rationale_summary: string;
  trigger_message_id?: string | null;
  estimated_cost?: number | null;
  permission_scopes: string[];
};
type SteeringResult = {
  status: "pending_confirmation" | "applied" | "rejected";
  envelope: SteeringEnvelopeView;
  impact?: {
    affected_nodes: string[];
    preserved_nodes: string[];
    invalidated_nodes: string[];
    earliest_checkpoint?: string | null;
  };
  message?: ChatMessageView | null;
  replacement_run_id?: string | null;
  target_run?: AgentTask;
};
type SteeringAudit = {
  id: string;
  status: string;
  replacement_task_id?: string | null;
  envelope: SteeringEnvelopeView;
};
type AgentInspection = {
  task: AgentTask;
  events: Array<{ sequence: number; type: string; payload: Record<string, unknown>; created_at: string }>;
  approvals: Array<{ id: string; action: string; status: string; scope: Record<string, unknown> }>;
};
type LiveEvent = { id: number; type: string; data: Record<string, unknown> };
type ActivityItem = AgentTask & { project_id: string; project_name: string };
type ProjectFile = {
  id: string;
  original_name: string;
  size_bytes: number;
  sha256: string;
};
type ArtifactLinkView = {
  id: string;
  link_id?: string;
  kind: string;
  mime_type: string;
  original_name: string;
  sha256: string;
  size_bytes: number;
  run_id?: string | null;
  validation_status: string;
  relation?: string;
  label?: string;
  display_order?: number;
  revision_id?: string | null;
  document_id?: string | null;
  delivery_status?: string;
  renderer_version?: string | null;
  created_at?: string;
  lineage?: { figure_artifact_ids?: string[] };
};
type PreviewTab = PreviewTabState<ArtifactLinkView>;
type KnowledgeHit = {
  item_id: string;
  title: string;
  content: string;
  source_uri: string | null;
  locator: Record<string, unknown>;
  trust_level: string;
  citation_policy: string;
  confidentiality: string;
  retrieval_reason: string;
};
type RequirementView = {
  requirement_id: string;
  requirement_version: number;
  status: string;
  raw_request: { text: string };
  normalized_request: string;
  research_formulation: Record<string, unknown>;
  confirmed_requirement: Record<string, unknown> | null;
  field_evidence: Record<string, { source_type: string; confidence: number; requires_confirmation: boolean }>;
  open_questions: Array<{ question_id: string; question: string; reason: string; affected_fields: string[]; priority: string }>;
  presentation?: {
    cover?: {
      enabled?: boolean | null;
      title?: string | null;
      subtitle?: string | null;
      fields?: Array<{ semantic_key: string; label: string; value: string }>;
    } | null;
    page_chrome?: Record<string, unknown> | null;
    unresolved?: Array<Record<string, unknown>>;
  };
  output_formats?: string[];
  template_id?: string | null;
};
type PlanNode = { node: string; reason: string; approval_required: boolean };
type RequirementResult = { requirement: RequirementView; plan_preview: PlanNode[]; outline?: { sections: Array<{ title: string; goal: string; target_length: number }> } };

function requirementPresentation(requirement: RequirementView) {
  const confirmed = requirement.confirmed_requirement;
  const candidate = confirmed && typeof confirmed === "object"
    ? confirmed as Partial<RequirementView>
    : requirement;
  return {
    presentation: candidate.presentation ?? requirement.presentation,
    outputFormats: candidate.output_formats ?? requirement.output_formats ?? [],
    templateId: candidate.template_id ?? requirement.template_id ?? null,
  };
}
type RecoveryItem = {
  id: string;
  description: string;
  state: string;
  action: string;
  paid: boolean;
  estimated_cost: number | null;
  next_action: string;
  graph_compatible: boolean;
};
type RecoveryCenter = { completed: number; stopped_at: string | null; requires_attention: boolean; pending: RecoveryItem[] };
type ProviderSetting = {
  id: string;
  display_name: string;
  modality: "text" | "image" | "embedding";
  protocol: string;
  provider_type: string;
  base_url: string;
  model: string;
  model_name: string;
  capabilities: string[];
  has_credential: boolean;
  credential_status: string;
  enabled: boolean;
  active: boolean;
  binding_id?: string | null;
  health_status: "unknown" | "healthy" | "degraded" | "error" | "blocked";
  health_detail: string;
  version: number;
  secret_version: number;
};
type ProviderBinding = {
  id: string;
  scope: string;
  scope_id?: string | null;
  modality: string;
  provider_id: string;
  version: number;
};
type ResourceLimitsView = {
  remote_llm: number;
  document_write: number;
  cpu_job: number;
  gpu_job: number;
  image_generation: number;
  render: number;
};
type RuntimeResourceSnapshot = { limits: ResourceLimitsView; used: ResourceLimitsView; queued: string[] };
type ToolStatus = {
  name: string;
  available: boolean;
  path: string | null;
  purpose: string;
  optional_size: string | null;
};
type FirstRunStatus = {
  completed: boolean;
  privacy_mode?: string;
  providers_configured?: boolean;
  skipped?: string[];
  tools: ToolStatus[];
  disk: { total: number; free: number; writable: boolean };
  gpu: { available: boolean; devices?: string[]; error?: string };
};
type DependencyInstallPlan = {
  tool: string;
  method: string;
  source: string;
  destination: string | null;
  estimated_bytes: number;
  requires_confirmation: boolean;
};
type DependencyInstallJob = {
  job_id: string;
  tool: string;
  status: string;
  destination: string | null;
  log_file: string;
  exit_code: number | null;
};

function isFirstRunStatus(value: unknown): value is FirstRunStatus {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<FirstRunStatus>;
  return Array.isArray(candidate.tools)
    && typeof candidate.disk === "object"
    && candidate.disk !== null
    && typeof candidate.gpu === "object"
    && candidate.gpu !== null;
}

const PROVIDER_PRESETS: Record<string, string> = {
  openai_compatible: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com/v1",
  gemini: "https://generativelanguage.googleapis.com/v1beta",
  deepseek: "https://api.deepseek.com/v1",
  doubao: "https://ark.cn-beijing.volces.com/api/v3",
  xiaomi_mimo: "https://api.xiaomimimo.com/v1",
  ollama: "http://127.0.0.1:11434/v1",
  custom_openai: "https://example.com/v1",
  openai_image: "https://api.openai.com/v1",
  seedream_image: "https://ark.cn-beijing.volces.com/api/v3",
  custom_image: "https://example.com/v1",
};

async function localApi<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error(`Local API failed: ${response.status}`);
  return (await response.json()) as T;
}

async function localToken(): Promise<string> {
  const response = await fetch("/api/bootstrap-token");
  return ((await response.json()) as { token: string }).token;
}

function elapsedLabel(startedAt: string | null | undefined, finishedAt: string | null | undefined, now: number): string {
  if (!startedAt) return "尚未开始";
  const elapsed = Math.max(0, new Date(finishedAt ?? now).getTime() - new Date(startedAt).getTime());
  const seconds = Math.floor(elapsed / 1000);
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function fileSizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function App() {
  const { previewOpen, drafts, togglePreview, saveDraft } = useWorkspace();
  const [sessionId, setSessionId] = useState("new");
  const [status, setStatus] = useState("本地模式 · 准备就绪");
  const [projects, setProjects] = useState<CreatedProject[]>([]);
  const [sessions, setSessions] = useState<CreatedSession[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [knowledgeOpen, setKnowledgeOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [recoveryOpen, setRecoveryOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const notifiedRuns = useRef(new Set<string>());
  const [recoveryCenter, setRecoveryCenter] = useState<RecoveryCenter | null>(null);
  const [providerId, setProviderId] = useState("");
  const [providerType, setProviderType] = useState("openai_compatible");
  const [providerUrl, setProviderUrl] = useState(PROVIDER_PRESETS.openai_compatible);
  const [providerModel, setProviderModel] = useState("");
  const [providerKey, setProviderKey] = useState("");
  const [imageProviderId, setImageProviderId] = useState("");
  const [imageProviderType, setImageProviderType] = useState("seedream_image");
  const [imageProviderUrl, setImageProviderUrl] = useState(PROVIDER_PRESETS.seedream_image);
  const [imageProviderModel, setImageProviderModel] = useState("");
  const [imageProviderKey, setImageProviderKey] = useState("");
  const [savedProviders, setSavedProviders] = useState<ProviderSetting[]>([]);
  const [providerBindings, setProviderBindings] = useState<ProviderBinding[]>([]);
  const [replacementKeys, setReplacementKeys] = useState<Record<string, string>>({});
  const [resourceLimits, setResourceLimits] = useState<ResourceLimitsView>({
    remote_llm: 4,
    document_write: 1,
    cpu_job: 2,
    gpu_job: 1,
    image_generation: 2,
    render: 1,
  });
  const [privacyMode, setPrivacyMode] = useState("standard");
  const [firstRun, setFirstRun] = useState<FirstRunStatus | null>(null);
  const [dependencyDestination, setDependencyDestination] = useState("");
  const [installJobs, setInstallJobs] = useState<Record<string, DependencyInstallJob>>({});
  const [memoryText, setMemoryText] = useState("");
  const [knowledgeFile, setKnowledgeFile] = useState<File | null>(null);
  const [knowledgeQuery, setKnowledgeQuery] = useState("");
  const [knowledgeHits, setKnowledgeHits] = useState<KnowledgeHit[]>([]);
  const [apiToken, setApiToken] = useState("");
  const [projectFiles, setProjectFiles] = useState<ProjectFile[]>([]);
  const [previewTabs, setPreviewTabs] = useState<PreviewTab[]>([]);
  const [activePreviewTabId, setActivePreviewTabId] = useState("");
  const [previewMaximized, setPreviewMaximized] = useState(false);
  const previewRequests = useRef(new Map<string, Promise<PreviewTab>>());
  const {
    previewWidth,
    setPreviewWidth,
    sidebarWidth,
    setSidebarWidth,
    sidebarCollapsed,
    setSidebarCollapsed,
  } = usePanePreferences();
  const [sidebarPeek, setSidebarPeek] = useState(false);
  const [lastRequest, setLastRequest] = useState("");
  const [requirementResult, setRequirementResult] = useState<RequirementResult | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessageView[]>([]);
  const [hasOlderMessages, setHasOlderMessages] = useState(false);
  const [viewingHistoryPage, setViewingHistoryPage] = useState(false);
  const [agentDebug, setAgentDebug] = useState<AgentTurnResult | null>(null);
  const [activeProjectId, setActiveProjectId] = useState("");
  const [runsById, setRunsById] = useState<Record<string, AgentTask>>({});
  const [activeTaskId, setActiveTaskId] = useState("");
  const [agentInspection, setAgentInspection] = useState<AgentInspection | null>(null);
  const [runEventsById, setRunEventsById] = useState<Record<string, LiveEvent[]>>({});
  const [steeringByRun, setSteeringByRun] = useState<Record<string, SteeringResult>>({});
  const runEventCursors = useRef<Record<string, number>>({});
  const [clock, setClock] = useState(() => Date.now());
  const draft = drafts[sessionId] ?? "";
  const activeTask = runsById[activeTaskId] ?? null;
  const activeTaskStatus = activeTask?.status ?? "";
  const liveEvents = activeTaskId ? runEventsById[activeTaskId] ?? [] : [];
  const activeProject = projects.find((project) => project.id === activeProjectId) ?? null;
  const activeSession = sessions.find((session) => session.id === sessionId) ?? null;
  const plannedPresentation = requirementResult
    ? requirementPresentation(requirementResult.requirement)
    : null;
  const sessionRuns = Object.values(runsById)
    .filter((run) => run.payload.session_id === sessionId)
    .sort((left, right) => (right.updated_at ?? "").localeCompare(left.updated_at ?? ""));
  const activeSessionRuns = sessionRuns.filter((run) => !TERMINAL_RUN_STATUSES.has(run.status));
  const primaryActiveRun = activeSessionRuns.find((run) => run.id === activeTaskId) ?? activeSessionRuns[0] ?? null;
  const steeringTarget = sessionRuns.find((run) =>
    ["pending", "running", "waiting_approval", "paused"].includes(run.status),
  ) ?? null;
  const activeRunIdsKey = Object.values(runsById)
    .filter((run) => run.payload.session_id && !["completed", "failed", "cancelled", "superseded"].includes(run.status))
    .map((run) => run.id)
    .sort()
    .join(",");

  function steeringTrace(runId: string) {
    const steering = steeringByRun[runId];
    if (!steering) return null;
    return (
      <details className="steering-trace" onClick={(event) => event.stopPropagation()}>
        <summary>
          {steering.envelope.impact_level} · {steering.envelope.relationship} · 查看干预依据
        </summary>
        <p>{steering.envelope.rationale_summary}</p>
        <dl>
          <div><dt>保留节点</dt><dd>{steering.envelope.preserved_nodes.join("、") || "无"}</dd></div>
          <div><dt>受影响节点</dt><dd>{steering.envelope.affected_nodes.join("、") || "无"}</dd></div>
        </dl>
        {steering.replacement_run_id ? (
          <button type="button" onClick={() => setActiveTaskId(steering.replacement_run_id!)}>
            跳转到替代任务
          </button>
        ) : null}
      </details>
    );
  }

  function startPaneResize(side: "left" | "right", event: React.PointerEvent): void {
    event.preventDefault();
    const move = (pointer: PointerEvent): void => {
      if (side === "left") {
        setSidebarWidth(Math.min(420, Math.max(190, pointer.clientX)));
      } else {
        const percentage = ((window.innerWidth - pointer.clientX) / window.innerWidth) * 100;
        setPreviewWidth(Math.min(65, Math.max(28, percentage)));
      }
    };
    const stop = (): void => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.classList.remove("is-resizing");
    };
    document.body.classList.add("is-resizing");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  }

  useEffect(() => {
    let active = true;
    async function loadProjects(): Promise<void> {
      try {
        const token = await localToken();
        if (active) setApiToken(token);
        const onboarding = await localApi<unknown>("/api/first-run", token).catch(() => null);
        if (active && isFirstRunStatus(onboarding)) {
          setFirstRun(onboarding);
          if (onboarding.privacy_mode) setPrivacyMode(onboarding.privacy_mode);
        }
        const loaded = await localApi<CreatedProject[]>("/api/projects", token);
        if (active) {
          setProjects(loaded);
          const route = window.location.pathname.match(/^\/projects\/([^/]+)\/conversations\/([^/]+)$/);
          const selectedProject = loaded.find((project) => project.id === route?.[1]) ?? loaded[0];
          if (selectedProject) {
            setActiveProjectId(selectedProject.id);
            const files = await localApi<ProjectFile[]>(`/api/projects/${selectedProject.id}/files`, token);
            if (active) setProjectFiles(files);
            const loadedSessions = await localApi<CreatedSession[]>(
              `/api/projects/${selectedProject.id}/conversations`,
              token,
            );
            if (active) setSessions(loadedSessions);
            const selectedSession = loadedSessions.find((session) => session.id === route?.[2]) ?? loadedSessions[0];
            if (active && selectedSession) {
              setSessionId(selectedSession.id);
              saveDraft(selectedSession.id, selectedSession.draft ?? "");
              const messages = await localApi<ChatMessageView[]>(
                `/api/projects/${selectedProject.id}/conversations/${selectedSession.id}/messages?before=${LATEST_MESSAGE_CURSOR}&limit=${MESSAGE_WINDOW_SIZE + 1}`,
                token,
              );
              if (active) {
                setChatMessages(boundedMessageWindow(messages));
                setHasOlderMessages(messages.length > MESSAGE_WINDOW_SIZE);
                setViewingHistoryPage(false);
              }
            }
            const previousTasks = await localApi<AgentTask[]>(
              `/api/projects/${selectedProject.id}/agent/tasks`,
              token,
            );
            if (active) {
              setRunsById(Object.fromEntries(previousTasks.map((task) => [task.id, task])));
              if (previousTasks[0]) setActiveTaskId(previousTasks[0].id);
              void restoreSteering(selectedProject.id, previousTasks, token);
            }
          }
        }
      } catch {
        // The static shell remains usable while the local backend starts.
      }
    }
    void loadProjects();
    return () => {
      active = false;
    };
  }, [saveDraft]);

  useEffect(() => {
    const running = Object.values(installJobs).filter((job) => job.status === "running");
    if (!apiToken || running.length === 0) return;
    const timer = window.setInterval(() => {
      void Promise.all(
        running.map((job) => localApi<DependencyInstallJob>(
          `/api/first-run/dependencies/jobs/${job.job_id}`,
          apiToken,
        )),
      ).then((jobs) => {
        setInstallJobs((current) => ({
          ...current,
          ...Object.fromEntries(jobs.map((job) => [job.tool, job])),
        }));
        if (jobs.some((job) => job.status === "completed")) {
          void localApi<unknown>("/api/first-run", apiToken).then((status) => {
            if (isFirstRunStatus(status)) setFirstRun(status);
          });
        }
      }).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [apiToken, installJobs]);

  useEffect(() => {
    if (!apiToken || !activeProjectId || sessionId === "new") return;
    const timer = window.setTimeout(() => {
      void localApi(
        `/api/projects/${activeProjectId}/conversations/${sessionId}`,
        apiToken,
        { method: "PATCH", body: JSON.stringify({ draft }) },
      ).catch(() => undefined);
    }, 500);
    return () => window.clearTimeout(timer);
  }, [activeProjectId, apiToken, draft, sessionId]);

  async function selectProject(projectId: string): Promise<void> {
    const token = apiToken || (await localToken());
    setActiveProjectId(projectId);
    setSessionId("new");
    setChatMessages([]);
    setHasOlderMessages(false);
    setViewingHistoryPage(false);
    setActiveTaskId("");
    setPreviewTabs([]);
    setActivePreviewTabId("");
    const [loadedSessions, files, projectTasks] = await Promise.all([
      localApi<CreatedSession[]>(`/api/projects/${projectId}/conversations`, token),
      localApi<ProjectFile[]>(`/api/projects/${projectId}/files`, token),
      localApi<AgentTask[]>(`/api/projects/${projectId}/agent/tasks`, token),
    ]);
    setSessions(loadedSessions);
    setProjectFiles(files);
    setRunsById((current) => ({
      ...current,
      ...Object.fromEntries(projectTasks.map((task) => [task.id, task])),
    }));
    await restoreSteering(projectId, projectTasks, token);
    if (loadedSessions[0]) {
      await selectConversation(projectId, loadedSessions[0].id, token);
    } else {
      setActiveTaskId("");
    }
  }

  async function restoreSteering(
    projectId: string,
    projectTasks: AgentTask[],
    token: string,
  ): Promise<void> {
    const terminal = projectTasks.filter((task) =>
      ["cancelled", "superseded"].includes(task.status),
    );
    const entries = await Promise.all(
      terminal.map(async (task) => {
        const decisions = await localApi<SteeringAudit[]>(
          `/api/projects/${projectId}/runs/${task.id}/steering`,
          token,
        );
        const latest = decisions.at(-1);
        return latest
          ? [
              task.id,
              {
                status: latest.status === "rejected" ? "rejected" : "applied",
                envelope: latest.envelope,
                replacement_run_id: latest.replacement_task_id,
              } satisfies SteeringResult,
            ] as const
          : null;
      }),
    );
    setSteeringByRun((current) => ({
      ...current,
      ...Object.fromEntries(entries.filter((entry) => entry !== null)),
    }));
  }

  async function selectConversation(projectId: string, conversationId: string, knownToken?: string): Promise<void> {
    const token = knownToken || apiToken || (await localToken());
    const messages = await localApi<ChatMessageView[]>(
      `/api/projects/${projectId}/conversations/${conversationId}/messages?before=${LATEST_MESSAGE_CURSOR}&limit=${MESSAGE_WINDOW_SIZE + 1}`,
      token,
    );
    setSessionId(conversationId);
    saveDraft(conversationId, sessions.find((item) => item.id === conversationId)?.draft ?? "");
    setChatMessages(boundedMessageWindow(messages));
    setHasOlderMessages(messages.length > MESSAGE_WINDOW_SIZE);
    setViewingHistoryPage(false);
    window.history.replaceState({}, "", `/projects/${projectId}/conversations/${conversationId}`);
    const projectTasks = await localApi<AgentTask[]>(`/api/projects/${projectId}/agent/tasks`, token);
    setRunsById((current) => ({
      ...current,
      ...Object.fromEntries(projectTasks.map((task) => [task.id, task])),
    }));
    setActiveTaskId(
      projectTasks.find((task) => task.payload.session_id === conversationId)?.id ?? "",
    );
    await restoreSteering(projectId, projectTasks, token);
  }

  async function loadOlderMessages(): Promise<void> {
    const oldest = chatMessages[0]?.sequence;
    if (!activeProjectId || sessionId === "new" || !oldest) return;
    const token = apiToken || (await localToken());
    const messages = await localApi<ChatMessageView[]>(
      `/api/projects/${activeProjectId}/conversations/${sessionId}/messages?before=${oldest}&limit=${MESSAGE_WINDOW_SIZE + 1}`,
      token,
    );
    setChatMessages(boundedMessageWindow(messages));
    setHasOlderMessages(messages.length > MESSAGE_WINDOW_SIZE);
    setViewingHistoryPage(true);
  }

  async function loadLatestMessages(): Promise<void> {
    if (!activeProjectId || sessionId === "new") return;
    const token = apiToken || (await localToken());
    const messages = await localApi<ChatMessageView[]>(
      `/api/projects/${activeProjectId}/conversations/${sessionId}/messages?before=${LATEST_MESSAGE_CURSOR}&limit=${MESSAGE_WINDOW_SIZE + 1}`,
      token,
    );
    setChatMessages(boundedMessageWindow(messages));
    setHasOlderMessages(messages.length > MESSAGE_WINDOW_SIZE);
    setViewingHistoryPage(false);
  }

  async function createProject(): Promise<void> {
    const name = window.prompt("项目名称");
    if (!name?.trim()) return;
    const token = apiToken || (await localToken());
    const project = await localApi<CreatedProject>("/api/projects", token, {
      method: "POST",
      body: JSON.stringify({ name: name.trim() }),
    });
    setProjects((current) => [project, ...current]);
    // Confirm the durable write immediately. Loading sessions, artifacts and
    // run history can take longer under a full E2E gate and must not hide the
    // fact that project creation itself already succeeded.
    setStatus("项目已创建，请新建会话后开始对话");
    await selectProject(project.id);
  }

  async function createConversation(): Promise<void> {
    if (!activeProjectId) {
      setStatus("请先新建项目");
      return;
    }
    const title = window.prompt("会话名称", "新对话");
    if (!title?.trim()) return;
    const token = apiToken || (await localToken());
    const conversation = await localApi<CreatedSession>(
      `/api/projects/${activeProjectId}/conversations`,
      token,
      { method: "POST", body: JSON.stringify({ title: title.trim() }) },
    );
    setSessions((current) => [conversation, ...current]);
    await selectConversation(activeProjectId, conversation.id, token);
    setStatus("新会话已创建");
  }

  useEffect(() => {
    if (!activeTaskId || !activeProjectId || !apiToken) return;
    if (["completed", "failed", "cancelled", "superseded"].includes(activeTaskStatus)) return;
    let stopped = false;
    const refresh = async (): Promise<void> => {
      try {
        const task = await localApi<AgentTask>(
          `/api/projects/${activeProjectId}/agent/tasks/${activeTaskId}`,
          apiToken,
        );
        if (stopped) return;
        setRunsById((current) => ({ ...current, [task.id]: task }));
        const inspection = await localApi<AgentInspection>(
          `/api/projects/${activeProjectId}/agent/tasks/${activeTaskId}/inspect`,
          apiToken,
        );
        if (!stopped) setAgentInspection(inspection);
        if (task.status === "completed" && task.payload.result) {
          const result = task.payload.result;
          setAgentDebug(result);
          setChatMessages((current) =>
            current.some((message) => message.id === result.message.id)
              ? current
              : boundedMessageWindow([...current, result.message]),
          );
          setStatus(`已完成 · ${result.rounds} 轮模型调用 · ${result.tool_call_count} 次工具调用`);
        } else if (task.status === "failed") {
          setStatus(`已保存；任务失败，可从检查器查看恢复信息：${task.payload.error ?? "未知错误"}`);
        } else if (task.status === "cancelled") {
          setStatus("任务已取消，已完成的事件和产物仍然保留");
        } else if (task.status === "superseded") {
          setStatus("原任务已由修订分支替代，历史输出仍可展开追溯");
        }
      } catch (error) {
        if (!stopped) setStatus(error instanceof Error ? error.message : "任务状态读取失败");
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 500);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [activeProjectId, activeTaskId, activeTaskStatus, apiToken]);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!activeProjectId || !apiToken) return;
    const projectRunIds = activeRunIdsKey ? activeRunIdsKey.split(",") : [];
    const controllers = projectRunIds.map(() => new AbortController());
    async function stream(runId: string, controller: AbortController): Promise<void> {
      try {
        const cursor = runEventCursors.current[runId] ?? 0;
        const response = await fetch(`/api/projects/${activeProjectId}/runs/${runId}/events/stream`, {
          headers: {
            Authorization: `Bearer ${apiToken}`,
            "Last-Event-ID": String(cursor),
          },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true }).replaceAll("\r\n", "\n");
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            if (!frame || frame.startsWith(":")) continue;
            const id = Number(frame.match(/^id:\s*(\d+)/m)?.[1] ?? 0);
            const type = frame.match(/^event:\s*(.+)$/m)?.[1]?.trim() ?? "message";
            const raw = frame.match(/^data:\s*(.+)$/m)?.[1];
            if (!id || !raw) continue;
            runEventCursors.current[runId] = Math.max(runEventCursors.current[runId] ?? 0, id);
            const data = JSON.parse(raw) as Record<string, unknown>;
            setRunEventsById((current) => {
              const events = current[runId] ?? [];
              if (events.some((event) => event.id === id)) return current;
              return { ...current, [runId]: mergePublicRunEvents(events, { id, type, data }) };
            });
          }
        }
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setStatus("实时事件流已断开，任务状态轮询仍在继续");
        }
      }
    }
    projectRunIds.forEach((runId, index) => void stream(runId, controllers[index]));
    return () => controllers.forEach((controller) => controller.abort());
  }, [activeProjectId, activeRunIdsKey, apiToken]);

  useEffect(() => {
    if (!apiToken) return;
    let stopped = false;
    const refresh = async (): Promise<void> => {
      try {
        const items = await localApi<ActivityItem[]>("/api/activity", apiToken);
        if (stopped) return;
        setActivity(items);
        for (const item of items) {
          if (
            item.unread &&
            ["completed", "failed"].includes(item.status) &&
            !notifiedRuns.current.has(item.id)
          ) {
            notifiedRuns.current.add(item.id);
            if (document.hidden && "Notification" in window && Notification.permission === "granted") {
              new Notification(`PaperAgent · ${item.project_name}`, {
                body: item.status === "completed" ? "后台任务已完成" : "后台任务需要处理",
              });
            }
          }
        }
      } catch {
        // Activity polling is advisory; durable run polling remains authoritative.
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [apiToken]);

  async function openActivityItem(item: ActivityItem): Promise<void> {
    const token = apiToken || (await localToken());
    await selectProject(item.project_id);
    if (item.payload.session_id) {
      await selectConversation(item.project_id, item.payload.session_id, token);
    }
    await localApi(`/api/projects/${item.project_id}/runs/${item.id}/read`, token, {
      method: "POST",
    });
    setActivity((current) => current.filter((candidate) => candidate.id !== item.id));
    setActivityOpen(false);
    if (previewOpen) togglePreview();
  }

  async function submit(): Promise<void> {
    if (!draft.trim()) return;
    if (!activeProjectId || sessionId === "new") {
      setStatus("请先手动新建项目和会话；发送消息不会自动创建项目");
      return;
    }
    const content = draft.trim();
    setStatus("正在保存到当前会话…");
    setLastRequest(content);
    try {
      const token = apiToken || (await localToken());
      const message = await localApi<ChatMessageView>(`/api/projects/${activeProjectId}/conversations/${sessionId}/messages`, token, {
        method: "POST",
        body: JSON.stringify({ role: "user", content }),
      });
      setChatMessages((current) => boundedMessageWindow([...current, message]));
      setViewingHistoryPage(false);
      saveDraft(sessionId, "");
      if (steeringTarget) {
        setStatus("正在判断新消息对当前任务的影响…");
        const path = `/api/projects/${activeProjectId}/runs/${steeringTarget.id}/steer`;
        let steering = await localApi<SteeringResult>(path, token, {
          method: "POST",
          body: JSON.stringify({
            content,
            message_id: message.id,
            provider_id: null,
          }),
        });
        if (steering.message) {
          setChatMessages((current) =>
            current.some((item) => item.id === steering.message?.id)
              ? current
              : boundedMessageWindow([...current, steering.message as ChatMessageView]),
          );
        }
        if (steering.status === "pending_confirmation") {
          const scope = steering.envelope.permission_scopes.length
            ? `\n权限范围：${steering.envelope.permission_scopes.join("、")}`
            : "";
          const cost = steering.envelope.estimated_cost == null
            ? ""
            : `\n预计成本：${steering.envelope.estimated_cost}`;
          const accepted = window.confirm(
            `${steering.envelope.impact_level} 变更需要确认\n${steering.envelope.rationale_summary}${scope}${cost}`,
          );
          steering = await localApi<SteeringResult>(path, token, {
            method: "POST",
            body: JSON.stringify({
              content,
              message_id: message.id,
              provider_id: null,
              decision_id: steering.envelope.decision_id,
              confirmed: accepted,
              rejected: !accepted,
            }),
          });
        }
        setSteeringByRun((current) => ({ ...current, [steeringTarget.id]: steering }));
        if (steering.message) {
          setChatMessages((current) =>
            current.some((item) => item.id === steering.message?.id)
              ? current
              : boundedMessageWindow([...current, steering.message as ChatMessageView]),
          );
        }
        if (steering.target_run) {
          setRunsById((current) => ({
            ...current,
            [steering.target_run!.id]: steering.target_run!,
          }));
        }
        const projectTasks = await localApi<AgentTask[]>(
          `/api/projects/${activeProjectId}/agent/tasks`,
          token,
        );
        setRunsById((current) => ({
          ...current,
          ...Object.fromEntries(projectTasks.map((task) => [task.id, task])),
        }));
        if (steering.replacement_run_id) setActiveTaskId(steering.replacement_run_id);
        setStatus(
          steering.status === "rejected"
            ? "已拒绝变更，原任务保持原计划"
            : `${steering.envelope.impact_level} Steering 已应用：${steering.envelope.rationale_summary}`,
        );
        return;
      }
      setStatus("已保存，可在刷新或重启后继续；正在启动 Agent…");
      try {
        setStatus("Agent 正在理解需求并选择工具…");
        const task = await localApi<AgentTask>(
          `/api/projects/${activeProjectId}/sessions/${sessionId}/agent/jobs`,
          token,
          {
            method: "POST",
            body: JSON.stringify({
              content,
              provider_id: null,
              approved: false,
              idempotency_key: `ui:${sessionId}:${message.id}`,
            }),
          },
        );
        setRunsById((current) => ({ ...current, [task.id]: task }));
        setActiveTaskId(task.id);
        setStatus(`已保存 · Agent 任务已启动 · ${task.id}`);
      } catch {
        setStatus("已保存；尚未配置或无法连接真实模型，Agent 可在配置后继续");
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "本地保存失败");
    }
  }

  async function controlAgent(action: "pause" | "resume" | "cancel"): Promise<void> {
    if (!activeTask || !activeProjectId) return;
    try {
      const token = apiToken || (await localToken());
      const task = await localApi<AgentTask>(
        `/api/projects/${activeProjectId}/agent/tasks/${activeTask.id}/${action}`,
        token,
        { method: "POST" },
      );
      setRunsById((current) => ({ ...current, [task.id]: task }));
      setStatus(
        action === "pause" ? "已请求在安全边界暂停" : action === "resume" ? "任务已恢复" : "任务已取消",
      );
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "任务控制失败");
    }
  }

  async function resumeWithSelection(optionId: string): Promise<void> {
    if (!activeTask || !activeProjectId || !apiToken) return;
    const task = await localApi<AgentTask>(
      `/api/projects/${activeProjectId}/agent/tasks/${activeTask.id}/resume`,
      apiToken,
      {
        method: "POST",
        body: JSON.stringify({ selection_id: optionId }),
      },
    );
    setRunsById((current) => ({ ...current, [task.id]: task }));
    setStatus("已保存选择，将从原任务的安全断点继续");
  }

  async function saveProvider(modality: "text" | "image"): Promise<void> {
    const fields = modality === "text"
      ? { id: providerId, type: providerType, url: providerUrl, model: providerModel, key: providerKey }
      : {
          id: imageProviderId,
          type: imageProviderType,
          url: imageProviderUrl,
          model: imageProviderModel,
          key: imageProviderKey,
        };
    if (!fields.url.trim() || !fields.model.trim() || !fields.id.trim()) {
      setStatus("请完整填写配置名称、API URL 和模型名");
      return;
    }
    const token = apiToken || (await localToken());
    const saved = await localApi<ProviderSetting>("/api/settings/providers", token, {
      method: "POST",
      body: JSON.stringify({
        id: fields.id,
        display_name: fields.id,
        modality,
        protocol: fields.type,
        provider_type: fields.type,
        base_url: fields.url,
        model: fields.model,
        api_key: fields.key || null,
        capabilities: modality === "text"
          ? ["chat", "stream", "tools", "structured_output"]
          : ["image_generation"],
      }),
    });
    if (modality === "text") {
      setProviderId("");
      setProviderModel("");
      setProviderKey("");
    } else {
      setImageProviderId("");
      setImageProviderModel("");
      setImageProviderKey("");
    }
    await loadProviderSettings(token);
    setStatus(`Provider ${saved.id} 已真实写入本地后端`);
  }

  async function savePrivacy(): Promise<void> {
    const token = apiToken || (await localToken());
    await localApi("/api/settings/privacy", token, {
      method: "PUT",
      body: JSON.stringify({ mode: privacyMode }),
    });
    setStatus("隐私模式已保存");
  }

  async function loadFirstRun(explicitToken?: string): Promise<void> {
    const token = explicitToken || apiToken || (await localToken());
    const status = await localApi<unknown>("/api/first-run", token);
    if (isFirstRunStatus(status)) setFirstRun(status);
  }

  async function installDependency(tool: string): Promise<void> {
    const token = apiToken || (await localToken());
    const request = {
      tool,
      destination: dependencyDestination.trim() || null,
      confirmed: false,
    };
    const plan = await localApi<DependencyInstallPlan>(
      "/api/first-run/dependencies/plan",
      token,
      { method: "POST", body: JSON.stringify(request) },
    );
    const gigabytes = (plan.estimated_bytes / 1024 ** 3).toFixed(2);
    const accepted = window.confirm(
      `将安装 ${tool}\n来源：${plan.source}\n目标：${plan.destination ?? "当前用户默认目录"}\n预留空间：${gigabytes} GB\n\n安装会在后台运行，是否继续？`,
    );
    if (!accepted) {
      setStatus(`已取消 ${tool} 安装，不影响基础功能`);
      return;
    }
    const job = await localApi<DependencyInstallJob>(
      "/api/first-run/dependencies/install",
      token,
      { method: "POST", body: JSON.stringify({ ...request, confirmed: true }) },
    );
    setInstallJobs((current) => ({ ...current, [tool]: job }));
    setStatus(`${tool} 已进入后台安装；可继续使用基础功能`);
  }

  async function completeFirstRun(): Promise<void> {
    const token = apiToken || (await localToken());
    const skipped = firstRun?.tools.filter((tool) => !tool.available).map((tool) => tool.name) ?? [];
    await localApi("/api/first-run/complete", token, {
      method: "POST",
      body: JSON.stringify({
        privacy_mode: privacyMode,
        providers_configured: savedProviders.some((provider) => provider.modality === "text" && provider.enabled),
        skipped,
      }),
    });
    await loadFirstRun(token);
    setStatus("首次运行设置已保存；可选依赖以后仍可安装");
  }

  async function loadProviderSettings(explicitToken?: string): Promise<void> {
    const token = explicitToken || apiToken || (await localToken());
    const [settings, bindings, resources] = await Promise.all([
      localApi<ProviderSetting[]>("/api/settings/providers", token),
      localApi<ProviderBinding[]>("/api/settings/provider-bindings", token),
      localApi<RuntimeResourceSnapshot>("/api/runtime/resources", token),
    ]);
    setSavedProviders(settings);
    setProviderBindings(bindings);
    setResourceLimits(resources.limits);
    await loadFirstRun(token).catch(() => undefined);
  }

  async function activateProvider(setting: ProviderSetting): Promise<void> {
    const token = apiToken || (await localToken());
    const binding = providerBindings.find((item) => item.modality === setting.modality && item.scope === "global");
    await localApi(`/api/settings/providers/${encodeURIComponent(setting.id)}/activate`, token, {
      method: "POST",
      body: JSON.stringify({ scope: "global", expected_version: binding?.version ?? null }),
    });
    await loadProviderSettings(token);
    setStatus(`已将 ${setting.id} 切换为当前${setting.modality === "text" ? "文本" : "生图"}模型；在途任务保持原快照`);
  }

  async function replaceProviderKey(setting: ProviderSetting): Promise<void> {
    const key = replacementKeys[setting.id]?.trim();
    if (!key) {
      setStatus("请输入新的 API Key");
      return;
    }
    const token = apiToken || (await localToken());
    await localApi<ProviderSetting>("/api/settings/providers", token, {
      method: "POST",
      body: JSON.stringify({
        id: setting.id,
        display_name: setting.display_name,
        modality: setting.modality,
        protocol: setting.protocol,
        provider_type: setting.provider_type,
        base_url: setting.base_url,
        model: setting.model,
        api_key: key,
        capabilities: setting.capabilities,
        version: setting.version,
      }),
    });
    setReplacementKeys((current) => ({ ...current, [setting.id]: "" }));
    await loadProviderSettings(token);
    setStatus(`${setting.id} 的密钥已安全轮换；旧密钥不会回显`);
  }

  async function disableProvider(setting: ProviderSetting): Promise<void> {
    if (!window.confirm(`停用 ${setting.id}？历史任务仍会保留其不可变配置快照。`)) return;
    const token = apiToken || (await localToken());
    const query = new URLSearchParams({
      confirmation: "DISABLE PROVIDER",
      expected_version: String(setting.version),
    });
    await localApi(`/api/settings/providers/${encodeURIComponent(setting.id)}?${query}`, token, {
      method: "DELETE",
    });
    await loadProviderSettings(token);
    setStatus(`${setting.id} 已停用`);
  }

  async function saveResourceLimits(): Promise<void> {
    const token = apiToken || (await localToken());
    const resources = await localApi<RuntimeResourceSnapshot>("/api/runtime/resources", token, {
      method: "PUT",
      body: JSON.stringify(resourceLimits),
    });
    setResourceLimits(resources.limits);
    setStatus("本机资源并发上限已保存；正在运行或排队时不会热改限制");
  }

  async function rememberPreference(): Promise<void> {
    if (!memoryText.trim()) return;
    const token = await localToken();
    await localApi("/api/memories", token, {
      method: "POST",
      body: JSON.stringify({
        scope: "long_term",
        kind: "preference",
        content: memoryText,
        source: "user",
        explicit: true,
      }),
    });
    setMemoryText("");
    setStatus("长期记忆已写入");
  }

  async function testProvider(setting: ProviderSetting): Promise<void> {
    if (!window.confirm("连接测试会向该 Provider 发送一条极短请求，可能产生少量费用。确认继续？")) return;
    const token = apiToken || (await localToken());
    setStatus(`正在测试 ${setting.id} 的真实连接…`);
    const response = await fetch(`/api/providers/${encodeURIComponent(setting.id)}/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ confirmation: "TEST PROVIDER" }),
    });
    const content = (await response.json()) as {
      model?: string;
      latency_ms?: number;
      detail?: string | { code?: string; detail?: string; action?: string };
    };
    const detail = typeof content.detail === "string"
      ? content.detail
      : [content.detail?.code, content.detail?.detail, content.detail?.action].filter(Boolean).join(" · ");
    setStatus(response.ok
      ? `连接成功：${content.model} · ${content.latency_ms} ms`
      : `连接失败：${detail || response.status}`);
    await loadProviderSettings(token);
  }

  async function clearMemory(): Promise<void> {
    if (!window.confirm("清空长期记忆后无法自动恢复，确定继续？")) return;
    const token = await localToken();
    await localApi("/api/memories/clear", token, {
      method: "POST",
      body: JSON.stringify({ scope: "long_term", confirmation: "CLEAR MEMORY" }),
    });
    setStatus("长期记忆已清空");
  }

  async function importKnowledge(): Promise<void> {
    const project = activeProject;
    if (!project || !knowledgeFile) {
      setStatus("请先创建项目并选择资料文件");
      return;
    }
    const token = await localToken();
    const form = new FormData();
    form.append("file", knowledgeFile);
    form.append("collection_id", "project");
    form.append("confidentiality", "personal");
    const response = await fetch(`/api/projects/${project.id}/knowledge/import`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    });
    if (!response.ok) throw new Error(`Knowledge import failed: ${response.status}`);
    const result = (await response.json()) as {
      indexed: number;
      classification: string;
      file_id: string;
    };
    const files = await localApi<ProjectFile[]>(`/api/projects/${project.id}/files`, token);
    setProjectFiles(files);
    setStatus(`已导入 ${result.indexed} 个知识块，分类：${result.classification}`);
  }

  async function openPreview(fileId: string, knownToken?: string): Promise<void> {
    const project = activeProject;
    if (!project) {
      setStatus("请先创建项目");
      return;
    }
    const requestKey = previewTabKey(project.id, `file:${fileId}`);
    let request = previewRequests.current.get(requestKey);
    if (!request) {
      setStatus("正在生成安全预览…");
      request = (async () => {
        const token = knownToken || apiToken || (await localToken());
        setApiToken(token);
        const artifact = await localApi<PreviewArtifact>(
          `/api/projects/${project.id}/preview/${fileId}`,
          token,
          { method: "POST" },
        );
        const result = await localApi<{ parts: PreviewPart[] }>(
          `/api/projects/${project.id}/preview/artifacts/${artifact.id}/parts?limit=100`,
          token,
        );
        return {
          id: requestKey,
          title: artifact.source_name,
          artifact,
          parts: result.parts,
          viewState: { ...DEFAULT_PREVIEW_VIEW_STATE },
        };
      })();
      previewRequests.current.set(requestKey, request);
    }
    let tab: PreviewTab;
    try {
      tab = await request;
    } finally {
      if (previewRequests.current.get(requestKey) === request) {
        previewRequests.current.delete(requestKey);
      }
    }
    setPreviewTabs((current) => upsertPreviewTab(current, tab));
    setActivePreviewTabId(requestKey);
    setKnowledgeOpen(false);
    setSettingsOpen(false);
    if (!previewOpen) togglePreview();
    setStatus(tab.artifact.status === "failed" ? "预览已安全降级" : "预览已就绪");
  }

  async function loadMorePreview(tabId = activePreviewTabId): Promise<void> {
    const project = activeProject;
    const tab = previewTabs.find((item) => item.id === tabId);
    if (!project || !tab || !apiToken) return;
    const result = await localApi<{ parts: PreviewPart[] }>(
      `/api/projects/${project.id}/preview/artifacts/${tab.artifact.id}/parts?offset=${tab.parts.length}&limit=100`,
      apiToken,
    );
    const next = [...tab.parts, ...result.parts];
    setPreviewTabs((tabs) => tabs.map((item) => (
      item.id === tabId ? { ...item, parts: next } : item
    )));
  }

  function selectPreviewTab(tabId: string): void {
    const tab = previewTabs.find((item) => item.id === tabId);
    if (!tab) return;
    setActivePreviewTabId(tab.id);
    setActivityOpen(false);
    setRecoveryOpen(false);
    setAgentOpen(false);
    setKnowledgeOpen(false);
    setSettingsOpen(false);
    if (!previewOpen) togglePreview();
  }

  function closePreviewTab(tabId: string): void {
    setPreviewTabs((current) => {
      const index = current.findIndex((item) => item.id === tabId);
      const next = current.filter((item) => item.id !== tabId);
      if (activePreviewTabId === tabId) {
        setActivePreviewTabId(next[Math.max(0, index - 1)]?.id ?? next[0]?.id ?? "");
      }
      return next;
    });
  }

  function movePreviewTab(direction: -1 | 1): void {
    if (!previewTabs.length) return;
    const index = Math.max(0, previewTabs.findIndex((item) => item.id === activePreviewTabId));
    const next = previewTabs[(index + direction + previewTabs.length) % previewTabs.length];
    if (next) selectPreviewTab(next.id);
  }

  async function openArtifactPreview(link: ArtifactLinkView): Promise<void> {
    if (!activeProject) return;
    const requestKey = previewTabKey(activeProject.id, link.id, link.revision_id);
    const existing = previewTabs.find((item) => item.id === requestKey);
    if (existing) {
      selectPreviewTab(existing.id);
      return;
    }
    let request = previewRequests.current.get(requestKey);
    if (!request) {
      setStatus(`正在预览 ${link.original_name}…`);
      request = (async () => {
        const token = apiToken || (await localToken());
        setApiToken(token);
        const artifact = await localApi<PreviewArtifact>(
          `/api/projects/${activeProject.id}/artifacts/${link.id}/structured-preview`,
          token,
          { method: "POST" },
        );
        const result = await localApi<{ parts: PreviewPart[] }>(
          `/api/projects/${activeProject.id}/preview/artifacts/${artifact.id}/parts?limit=100`,
          token,
        );
        return {
          id: requestKey,
          title: link.original_name,
          artifact,
          parts: result.parts,
          delivery: link,
          viewState: { ...DEFAULT_PREVIEW_VIEW_STATE },
        };
      })();
      previewRequests.current.set(requestKey, request);
    }
    let tab: PreviewTab;
    try {
      tab = await request;
    } finally {
      if (previewRequests.current.get(requestKey) === request) {
        previewRequests.current.delete(requestKey);
      }
    }
    setPreviewTabs((current) => upsertPreviewTab(current, tab));
    setActivePreviewTabId(requestKey);
    setActivityOpen(false);
    setRecoveryOpen(false);
    setAgentOpen(false);
    setKnowledgeOpen(false);
    setSettingsOpen(false);
    if (!previewOpen) togglePreview();
    setStatus("预览已就绪");
  }

  function updatePreviewViewState(tabId: string, patch: Partial<PreviewViewState>): void {
    setPreviewTabs((current) => current.map((item) => (
      item.id === tabId
        ? { ...item, viewState: { ...item.viewState, ...patch } }
        : item
    )));
  }

  async function downloadArtifact(link: ArtifactLinkView): Promise<void> {
    if (!activeProject) return;
    try {
      setStatus(`正在下载 ${link.original_name}…`);
      const token = apiToken || (await localToken());
      setApiToken(token);
      const response = await fetch(
        `/api/projects/${activeProject.id}/artifacts/${link.id}/download`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const total = Number(response.headers.get("Content-Length") ?? link.size_bytes);
      let blob: Blob;
      if (response.body) {
        const reader = response.body.getReader();
        const chunks: Uint8Array<ArrayBuffer>[] = [];
        let received = 0;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = value.slice();
          chunks.push(chunk);
          received += chunk.byteLength;
          setStatus(
            total > 0
              ? `正在下载 ${link.original_name} · ${Math.min(100, Math.round((received / total) * 100))}%`
              : `正在下载 ${link.original_name} · ${received} B`,
          );
        }
        blob = new Blob(chunks, { type: link.mime_type });
      } else {
        blob = await response.blob();
      }
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = link.original_name;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
      setStatus(`已下载 ${link.original_name}`);
    } catch (error) {
      setStatus(`下载失败：${error instanceof Error ? error.message : "未知错误"}`);
    }
  }

  async function searchKnowledge(): Promise<void> {
    const project = activeProject;
    if (!project || !knowledgeQuery.trim()) return;
    const token = await localToken();
    const result = await localApi<{ hits: KnowledgeHit[] }>(
      `/api/projects/${project.id}/knowledge/search?q=${encodeURIComponent(knowledgeQuery)}`,
      token,
    );
    setKnowledgeHits(result.hits);
    setStatus(`检索到 ${result.hits.length} 条可追溯结果`);
  }

  async function deleteKnowledge(itemId: string): Promise<void> {
    const project = activeProject;
    if (!project || !window.confirm("删除知识条目会影响后续检索，确定继续？")) return;
    const token = await localToken();
    await localApi(
      `/api/projects/${project.id}/knowledge/${itemId}?confirmation=${encodeURIComponent("DELETE KNOWLEDGE")}`,
      token,
      { method: "DELETE" },
    );
    setKnowledgeHits((items) => items.filter((item) => item.item_id !== itemId));
    setStatus("知识条目已删除");
  }

  async function correctKnowledge(itemId: string): Promise<void> {
    const project = activeProject;
    if (!project) return;
    const token = await localToken();
    await localApi(`/api/projects/${project.id}/knowledge/${itemId}/classification`, token, {
      method: "PATCH",
      body: JSON.stringify({ content_type: "manual" }),
    });
    setStatus("分类已修正为操作手册，原分类已留档");
  }

  async function rebuildKnowledge(): Promise<void> {
    const project = activeProject;
    if (!project) return;
    const token = await localToken();
    const result = await localApi<{ indexed: number }>(
      `/api/projects/${project.id}/knowledge/rebuild`,
      token,
      { method: "POST" },
    );
    setStatus(`知识索引已重建：${result.indexed} 条`);
  }

  async function analyzeRequirement(): Promise<void> {
    const project = activeProject;
    const text = draft.trim() || lastRequest.trim();
    if (!project || !text) {
      setStatus("请先创建项目并输入需求");
      return;
    }
    const token = apiToken || (await localToken());
    const result = await localApi<RequirementResult>(
      `/api/projects/${project.id}/requirements/analyze`,
      token,
      { method: "POST", body: JSON.stringify({ text }) },
    );
    setRequirementResult(result);
    setAgentOpen(true);
    setActivityOpen(false);
    setRecoveryOpen(false);
    setKnowledgeOpen(false);
    setSettingsOpen(false);
    if (!previewOpen) togglePreview();
    setStatus(result.requirement.status === "needs_input" ? "需求需要澄清" : "需求等待确认");
  }

  async function confirmRequirement(): Promise<void> {
    const project = activeProject;
    if (!project || !requirementResult) return;
    const token = apiToken || (await localToken());
    const result = await localApi<RequirementResult>(
      `/api/projects/${project.id}/requirements/confirm`,
      token,
      { method: "POST", body: JSON.stringify({ requirement: requirementResult.requirement }) },
    );
    setRequirementResult(result);
    setStatus("Requirement Spec 已确认，框架与 Agent 计划已冻结");
  }

  async function loadRecovery(): Promise<void> {
    const token = apiToken || (await localToken());
    const parameters = new URLSearchParams();
    if (activeProjectId) parameters.set("project_id", activeProjectId);
    const recoveryTask =
      sessionRuns.find((run) => run.id === activeTaskId) ?? sessionRuns[0] ?? null;
    if (activeProjectId && recoveryTask) parameters.set("task_id", recoveryTask.id);
    const scope = parameters.size ? `?${parameters.toString()}` : "";
    const center = await localApi<RecoveryCenter>(`/api/recovery${scope}`, token);
    setRecoveryCenter(center);
    setRecoveryOpen(true);
    setActivityOpen(false);
    setAgentOpen(false);
    setKnowledgeOpen(false);
    setSettingsOpen(false);
    if (!previewOpen) togglePreview();
  }

  async function decideRecovery(id: string, decision: "retry" | "skip"): Promise<void> {
    if (!window.confirm(decision === "retry" ? "确认继续此操作？付费 API 或代码可能产生费用与风险。" : "确认跳过此操作？")) return;
    const token = apiToken || (await localToken());
    await localApi(`/api/recovery/${id}/decision`, token, { method: "POST", body: JSON.stringify({ decision }) });
    await loadRecovery();
    setStatus(decision === "retry" ? "已按用户确认恢复任务" : "已跳过中断任务");
  }

  function resolveQuestion(questionId: string, accepted: boolean): void {
    setRequirementResult((current) => {
      if (!current) return current;
      const question = current.requirement.open_questions.find((item) => item.question_id === questionId);
      const remaining = current.requirement.open_questions.filter((item) => item.question_id !== questionId);
      const requirement = structuredClone(current.requirement) as RequirementView & Record<string, unknown>;
      if (!accepted && question) {
        const path = question.affected_fields[0]?.split(".")[0];
        if (path && path !== "conflicts") requirement[path] = null;
      }
      requirement.open_questions = remaining;
      requirement.status = remaining.length ? "needs_input" : "awaiting_confirmation";
      return { ...current, requirement };
    });
    setStatus(accepted ? "已接受字段候选" : "已拒绝字段候选，需要补充新值");
  }

  return (
    <main
      className="workspace"
      data-preview-open={previewOpen}
      data-sidebar-collapsed={sidebarCollapsed}
      data-sidebar-peek={sidebarPeek}
      data-preview-maximized={previewMaximized}
      style={{
        "--sidebar-width": `${sidebarWidth}px`,
        "--preview-width": `calc((100vw - ${sidebarCollapsed ? 0 : sidebarWidth}px) * ${previewWidth / 100})`,
      } as CSSProperties}
    >
      {sidebarCollapsed ? (
        <div
          className="sidebar-peek-zone"
          aria-hidden="true"
          onMouseEnter={() => setSidebarPeek(true)}
        />
      ) : null}
      <aside
        className="sidebar"
        aria-label="项目与历史会话"
        onMouseLeave={() => sidebarCollapsed && setSidebarPeek(false)}
      >
        <div className="sidebar-heading">
          <div className="brand">PaperAgent</div>
          <button
            type="button"
            className="icon-button"
            aria-label="隐藏左侧栏"
            title="隐藏左侧栏"
            onClick={() => {
              setSidebarCollapsed(true);
              setSidebarPeek(false);
            }}
          >
            ‹
          </button>
        </div>
        <button type="button" className="new-task" onClick={() => void createProject()}>
          ＋ 新建项目
        </button>
        <button type="button" className="new-conversation" onClick={() => void createConversation()} disabled={!activeProjectId}>
          ＋ 新建会话
        </button>
        <nav>
          <p className="section-label">最近项目</p>
          {projects.length ? (
            projects.map((project) => (
              <div className="project-nav-group" key={project.id}>
                <button
                  type="button"
                  className={`nav-item${project.id === activeProjectId ? " nav-item--active" : ""}`}
                  onClick={() => void selectProject(project.id)}
                >
                  <span>{project.name}</span>
                  {activity.some((item) => item.project_id === project.id && item.unread) ? (
                    <span className="activity-badge" aria-label="有未读任务">●</span>
                  ) : null}
                </button>
                {project.id === activeProjectId ? sessions.map((conversation) => (
                  <button
                    type="button"
                    className={`conversation-nav-item${conversation.id === sessionId ? " conversation-nav-item--active" : ""}`}
                    key={conversation.id}
                    onClick={() => void selectConversation(project.id, conversation.id)}
                  >
                    {conversation.title}
                  </button>
                )) : null}
              </div>
            ))
          ) : (
            <p className="empty-projects">暂无项目</p>
          )}
        </nav>
        <div
          className="pane-resizer pane-resizer--left"
          role="separator"
          aria-label="调整左侧栏宽度"
          aria-orientation="vertical"
          tabIndex={0}
          onPointerDown={(event) => startPaneResize("left", event)}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") setSidebarWidth((value) => Math.max(190, value - 10));
            if (event.key === "ArrowRight") setSidebarWidth((value) => Math.min(420, value + 10));
          }}
        />
      </aside>

      <section className="conversation" aria-label="任务工作区">
        <header className="topbar">
          <div className="topbar-context">
            <strong>{activeSession?.title ?? activeProject?.name ?? "PaperAgent"}</strong>
            <span>{status}</span>
          </div>
          <div className="topbar-actions">
            {sidebarCollapsed ? (
              <button
                type="button"
                aria-label="显示左侧栏"
                onClick={() => {
                  setSidebarCollapsed(false);
                  setSidebarPeek(false);
                }}
              >
                显示侧栏
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => {
                setKnowledgeOpen(false);
                setSettingsOpen(false);
                setAgentOpen(false);
                setRecoveryOpen(false);
                setActivityOpen(false);
                togglePreview();
              }}
            >
              {previewOpen ? "关闭预览" : "打开预览"}
            </button>
            <button
              type="button"
              onClick={() => {
                setActivityOpen(true);
                setSettingsOpen(false);
                setKnowledgeOpen(false);
                setAgentOpen(false);
                setRecoveryOpen(false);
                if (!previewOpen) togglePreview();
              }}
            >
              活动{activity.filter((item) => item.unread).length ? ` ${activity.filter((item) => item.unread).length}` : ""}
            </button>
            <button
              type="button"
              onClick={() => {
                setSettingsOpen(true);
                setActivityOpen(false);
                setKnowledgeOpen(false);
                setAgentOpen(false);
                setRecoveryOpen(false);
                if (!previewOpen) togglePreview();
                void loadProviderSettings();
              }}
            >
              设置
            </button>
            <button
              type="button"
              onClick={() => {
                setKnowledgeOpen(true);
                setActivityOpen(false);
                setSettingsOpen(false);
                setAgentOpen(false);
                setRecoveryOpen(false);
                if (!previewOpen) togglePreview();
              }}
            >
              知识库
            </button>
            <button type="button" onClick={() => void analyzeRequirement()}>
              Agent 计划
            </button>
            <button type="button" onClick={() => void loadRecovery()}>
              恢复中心
            </button>
          </div>
        </header>
        {firstRun && !firstRun.completed ? (
          <button
            type="button"
            className="onboarding-banner"
            onClick={() => {
              setSettingsOpen(true);
              setActivityOpen(false);
              setKnowledgeOpen(false);
              setAgentOpen(false);
              setRecoveryOpen(false);
              if (!previewOpen) togglePreview();
              void loadProviderSettings();
            }}
          >
            <span>首次运行检查尚未完成</span>
            <small>配置隐私、文本模型和可选排版工具；跳过可选项不影响基础功能</small>
          </button>
        ) : null}
        <div className={chatMessages.length ? "message-list" : "empty-state"}>
          {chatMessages.length ? (
            <>
              {hasOlderMessages || viewingHistoryPage ? (
                <div className="message-history-toolbar">
                  {hasOlderMessages ? (
                    <button type="button" onClick={() => void loadOlderMessages()}>
                      查看更早的 {MESSAGE_WINDOW_SIZE} 条
                    </button>
                  ) : <span>已到达当前历史页起点</span>}
                  {viewingHistoryPage ? (
                    <button type="button" onClick={() => void loadLatestMessages()}>
                      回到最新消息
                    </button>
                  ) : null}
                </div>
              ) : null}
              {chatMessages.map((message) => {
              const sourceRun = message.run_id ? runsById[message.run_id] : null;
              const folded = sourceRun && ["cancelled", "superseded"].includes(sourceRun.status);
              const deliveredLinks = (message.artifact_links ?? []).filter((artifact) => (
                artifact.validation_status === "valid"
                && artifact.delivery_status !== "rejected"
              ));
              const body = (
                <article className={`message message--${message.role}`}>
                  <span>{message.role === "user" ? "你" : "PaperAgent"}</span>
                  {sourceRun && message.role === "assistant" && TERMINAL_RUN_STATUSES.has(sourceRun.status) ? (
                    <HistoricalRunTrace
                      run={sourceRun}
                      events={runEventsById[sourceRun.id] ?? []}
                      elapsed={elapsedLabel(sourceRun.started_at, sourceRun.finished_at, clock)}
                      onSelect={() => setActiveTaskId(sourceRun.id)}
                    >
                      {steeringTrace(sourceRun.id)}
                    </HistoricalRunTrace>
                  ) : null}
                  <Suspense fallback={<div className="markdown-content">正在渲染内容…</div>}>
                    <MarkdownContent content={message.content} />
                  </Suspense>
                  {deliveredLinks.length ? (
                    <div className="artifact-cards" aria-label="本轮生成的文件">
                      {deliveredLinks.map((artifact) => (
                        <article
                          className={artifact.validation_status === "valid"
                            ? "artifact-card"
                            : "artifact-card artifact-card--invalid"}
                          draggable
                          key={artifact.link_id ?? artifact.id}
                          onDragStart={(event) => {
                            event.dataTransfer.setData(
                              "application/x-paperagent-artifact",
                              artifact.id,
                            );
                            event.dataTransfer.effectAllowed = "copy";
                          }}
                        >
                          <button
                            type="button"
                            className="artifact-card__main"
                            disabled={artifact.validation_status !== "valid"}
                            onClick={() => void openArtifactPreview(artifact)}
                          >
                            <span className="artifact-card__icon" aria-hidden="true">▤</span>
                            <span>
                              <strong>{artifact.original_name}</strong>
                              <small>
                                {artifact.kind} · {fileSizeLabel(artifact.size_bytes)} · {
                                  artifact.delivery_status === "delivered" ? "已交付" : "已校验"
                                }
                              </small>
                              {artifact.revision_id ? (
                                <small>
                                  来源 {artifact.revision_id.slice(0, 8)} · {artifact.renderer_version ?? "原生"}
                                  {artifact.lineage?.figure_artifact_ids
                                    ? ` · 图片 ${artifact.lineage.figure_artifact_ids.length}`
                                    : ""}
                                </small>
                              ) : null}
                            </span>
                          </button>
                          <button
                            type="button"
                            className="artifact-card__download"
                            disabled={artifact.validation_status !== "valid"}
                            aria-label={`下载 ${artifact.original_name}`}
                            onClick={() => void downloadArtifact(artifact)}
                          >
                            下载
                          </button>
                          {artifact.run_id ? (
                            <button
                              type="button"
                              className="artifact-card__download"
                              aria-label={`查看 ${artifact.original_name} 的运行轨迹`}
                              onClick={() => {
                                setActiveTaskId(artifact.run_id ?? "");
                                setActivityOpen(true);
                                if (!previewOpen) togglePreview();
                              }}
                            >
                              运行
                            </button>
                          ) : null}
                        </article>
                      ))}
                    </div>
                  ) : null}
                </article>
              );
              return folded ? (
                <details className="message-fold" key={message.id}>
                  <summary>
                    {sourceRun.status === "superseded" ? "已被修订替代的输出" : "已终止任务的输出"}
                    <span>点击展开追溯</span>
                  </summary>
                  {body}
                </details>
              ) : (
                <div key={message.id}>{body}</div>
              );
              })}
            </>
          ) : (
            <>
              <span className="status-dot" />
              <h1>PaperAgent 已准备好</h1>
              <p>输入需求后将展示真实模型、工具、计划与可追溯产物。</p>
            </>
          )}
        </div>
        {primaryActiveRun ? (
          <div className="active-run-slot" aria-live="polite">
            <ActiveRunCard
              run={primaryActiveRun}
              events={runEventsById[primaryActiveRun.id] ?? []}
              elapsed={elapsedLabel(primaryActiveRun.started_at, primaryActiveRun.finished_at, clock)}
              firstFeedback={primaryActiveRun.first_output_at && primaryActiveRun.started_at
                ? elapsedLabel(primaryActiveRun.started_at, primaryActiveRun.first_output_at, clock)
                : null}
              selected={primaryActiveRun.id === activeTaskId}
              onSelect={() => setActiveTaskId(primaryActiveRun.id)}
            >
              {steeringTrace(primaryActiveRun.id)}
            </ActiveRunCard>
            {activeSessionRuns.length > 1 ? (
              <button type="button" className="active-run-count" onClick={() => setActivityOpen(true)}>
                另有 {activeSessionRuns.length - 1} 个后台任务
              </button>
            ) : null}
          </div>
        ) : null}
        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label htmlFor="prompt">描述你的论文或报告需求</label>
          <textarea
            id="prompt"
            rows={3}
            value={draft}
            onChange={(event) => saveDraft(sessionId, event.target.value)}
            placeholder="输入需求，或添加 PDF、Word、代码与模板…"
          />
          <div className="composer-actions">
            {activeTask?.status === "running" ? (
              <button type="button" className="secondary" onClick={() => void controlAgent("pause")}>暂停</button>
            ) : null}
            {activeTask?.status === "paused" ? (
              <button type="button" className="secondary" onClick={() => void controlAgent("resume")}>继续</button>
            ) : null}
            {activeTask && ["pending", "running", "paused"].includes(activeTask.status) ? (
              <button type="button" className="danger" onClick={() => void controlAgent("cancel")}>取消</button>
            ) : null}
            <button type="submit">发送</button>
          </div>
        </form>
      </section>

      <aside
        className="preview"
        aria-label="文件预览"
        onDragOver={(event) => {
          if (event.dataTransfer.types.includes("application/x-paperagent-artifact")) {
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
          }
        }}
        onDrop={(event) => {
          const artifactId = event.dataTransfer.getData(
            "application/x-paperagent-artifact",
          );
          const artifact = chatMessages
            .flatMap((message) => message.artifact_links ?? [])
            .find((item) => item.id === artifactId);
          if (artifact) {
            event.preventDefault();
            void openArtifactPreview(artifact);
          }
        }}
      >
        <div
          className="pane-resizer pane-resizer--right"
          role="separator"
          aria-label="调整右侧预览宽度"
          aria-orientation="vertical"
          tabIndex={previewOpen ? 0 : -1}
          onPointerDown={(event) => startPaneResize("right", event)}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") setPreviewWidth((value) => Math.min(65, value + 2));
            if (event.key === "ArrowRight") setPreviewWidth((value) => Math.max(28, value - 2));
          }}
        />
        <header>
          <strong>{activityOpen ? "全局活动" : recoveryOpen ? "恢复中心" : agentOpen ? "Agent 计划" : knowledgeOpen ? "项目知识库" : settingsOpen ? "设置" : "产物预览"}</strong>
          <span className="preview-header-actions">
            {!activityOpen && !recoveryOpen && !agentOpen && !knowledgeOpen && !settingsOpen ? (
              <button
                type="button"
                onClick={() => setPreviewMaximized((value) => !value)}
                aria-label={previewMaximized ? "恢复预览面板" : "最大化预览面板"}
              >
                {previewMaximized ? "恢复" : "最大化"}
              </button>
            ) : null}
            <button type="button" onClick={() => { setPreviewMaximized(false); togglePreview(); }} aria-label="关闭文件预览">
              ×
            </button>
          </span>
        </header>
        {!activityOpen
          && !recoveryOpen
          && !agentOpen
          && !knowledgeOpen
          && !settingsOpen
          && previewTabs.length ? (
            <nav
              className="preview-tabs"
              aria-label="已打开的预览文件"
              onKeyDown={(event) => {
                if (event.key === "ArrowLeft") { event.preventDefault(); movePreviewTab(-1); }
                if (event.key === "ArrowRight") { event.preventDefault(); movePreviewTab(1); }
              }}
            >
              {previewTabs.map((tab) => (
                <div
                  className={tab.id === activePreviewTabId
                    ? "preview-tab preview-tab--active"
                    : "preview-tab"}
                  key={tab.id}
                >
                  <button
                    type="button"
                    aria-current={tab.id === activePreviewTabId ? "page" : undefined}
                    onClick={() => selectPreviewTab(tab.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Delete") closePreviewTab(tab.id);
                    }}
                  >
                    {tab.title}
                  </button>
                  <button
                    type="button"
                    aria-label={`关闭 ${tab.title}`}
                    onClick={() => closePreviewTab(tab.id)}
                  >
                    ×
                  </button>
                </div>
              ))}
            </nav>
          ) : null}
        {activityOpen ? (
          <div className="settings-panel activity-panel" aria-live="polite">
            <h2>后台任务</h2>
            <p>切换项目或会话不会中断正在运行和排队的任务。</p>
            {activity.length ? activity.map((item) => (
              <button
                type="button"
                className="activity-item"
                key={`${item.project_id}:${item.id}`}
                onClick={() => void openActivityItem(item)}
              >
                <span>
                  <strong>{item.project_name}</strong>
                  <small>{item.current_phase ?? item.status} · {item.status}</small>
                </span>
                {item.unread ? <em>未读</em> : <time>{item.attempt ?? 0} 次尝试</time>}
              </button>
            )) : <p>当前没有后台或未读任务。</p>}
          </div>
        ) : recoveryOpen ? (
          <div className="settings-panel recovery-panel" aria-live="polite">
            <h2>断点恢复</h2>
            <p>{recoveryCenter?.stopped_at ? `上次停止于：${recoveryCenter.stopped_at}` : "没有待恢复任务"}</p>
            <p>已完成副作用：{recoveryCenter?.completed ?? 0}</p>
            {recoveryCenter?.pending.map((item) => (
              <article className="agent-card" key={item.id}>
                <strong>{item.description}</strong>
                <p>状态：{item.state} · 类型：{item.action}</p>
                <p>{item.next_action}</p>
                {item.paid ? <em>可能费用：{item.estimated_cost ?? "Provider 未返回估算"}</em> : null}
                {!item.graph_compatible ? <p role="alert">此任务来自不兼容的流程版本，请先升级迁移。</p> : null}
                <div>
                  <button type="button" disabled={!item.graph_compatible} onClick={() => void decideRecovery(item.id, "retry")}>继续/重试</button>
                  <button type="button" onClick={() => void decideRecovery(item.id, "skip")}>跳过</button>
                </div>
              </article>
            ))}
            {activeTask?.payload.failure_category ? (
              <article className="agent-card delivery-recovery-card" role="alert">
                <strong>文档交付需要处理</strong>
                <p>{publicFailureMessage(
                  activeTask.payload.failure_category,
                  activeTask.payload.failure_details,
                )}</p>
                {activeTask.recovery_strategy ? (
                  <small>恢复策略：{activeTask.recovery_strategy}</small>
                ) : null}
                {activeTask.payload.recovery_options?.length ? (
                  <div className="recovery-options" role="list" aria-label="恢复候选">
                    {activeTask.payload.recovery_options.map((option) => (
                      <button
                        type="button"
                        role="listitem"
                        key={option.id}
                        onClick={() => void resumeWithSelection(option.id)}
                      >
                        {option.thumbnail_url ? <img src={option.thumbnail_url} alt="" /> : null}
                        <span>
                          <strong>{option.title}</strong>
                          <small>{option.subtitle ?? option.source_run_id ?? "当前任务候选"}</small>
                          {option.hash ? <code>{option.hash.slice(0, 8)}</code> : null}
                        </span>
                      </button>
                    ))}
                  </div>
                ) : null}
              </article>
            ) : null}
            <p>启动时不会自动重发付费 API，也不会自动恢复代码执行。</p>
          </div>
        ) : agentOpen && requirementResult ? (
          <div className="settings-panel agent-panel">
            <h2>需求四层表示</h2>
            <article className="agent-card"><strong>1. 用户原文（不可修改）</strong><p>{requirementResult.requirement.raw_request.text}</p></article>
            <article className="agent-card"><strong>2. 规范化需求</strong><p>{requirementResult.requirement.normalized_request}</p></article>
            <article className="agent-card"><strong>3. 科学化候选</strong><pre>{JSON.stringify(requirementResult.requirement.research_formulation, null, 2)}</pre></article>
            <article className="agent-card"><strong>4. 已确认执行版</strong><pre>{requirementResult.requirement.confirmed_requirement ? JSON.stringify(requirementResult.requirement.confirmed_requirement, null, 2) : "尚未确认"}</pre></article>
            {plannedPresentation?.presentation ? (
              <details className="agent-card presentation-plan-summary">
                <summary>
                  <strong>文档呈现确认</strong>
                  <span>{plannedPresentation.presentation.cover?.fields?.length ?? 0} 个封面字段 · 默认折叠</span>
                </summary>
                <p>执行前确认封面、个人信息和页眉页脚；这些设置会进入 canonical document revision。</p>
                <div className="presentation-plan-grid">
                  <span>封面：{plannedPresentation.presentation.cover?.enabled === false ? "关闭" : "启用"}</span>
                  <span>模板：{plannedPresentation.templateId ?? "使用文档类型默认配置"}</span>
                  <span>格式：{plannedPresentation.outputFormats.length ? plannedPresentation.outputFormats.join(" / ").toUpperCase() : "按用户后续选择"}</span>
                  <span>能力：DOCX/PDF 原生分页；Markdown 保留语义结构</span>
                </div>
                {plannedPresentation.presentation.cover?.fields?.length ? (
                  <dl className="presentation-plan-fields">
                    {plannedPresentation.presentation.cover.fields.map((field) => (
                      <div key={field.semantic_key}><dt>{field.label}</dt><dd>{field.value}</dd></div>
                    ))}
                  </dl>
                ) : <p>尚未识别到封面字段；如用户需要姓名、学校或自定义信息，将在执行前澄清。</p>}
                {plannedPresentation.presentation.page_chrome ? (
                  <pre>{JSON.stringify(plannedPresentation.presentation.page_chrome, null, 2)}</pre>
                ) : <p>未指定页眉页脚，将采用文档类型默认策略。</p>}
              </details>
            ) : null}
            <p className="agent-status">状态：{requirementResult.requirement.status} · v{requirementResult.requirement.requirement_version}</p>
            {agentDebug ? (
              <article className="agent-card">
                <strong>真实 Agent 内核轨迹</strong>
                <pre>{JSON.stringify({
                  task_id: agentDebug.task_id,
                  rounds: agentDebug.rounds,
                  tool_call_count: agentDebug.tool_call_count,
                  routes: agentDebug.routes,
                  prompt: agentDebug.prompt,
                }, null, 2)}</pre>
              </article>
            ) : null}
            {activeTask ? (
              <article className="agent-card">
                <strong>运行时状态与断点</strong>
                <pre>{JSON.stringify({
                  task_id: activeTask.id,
                  status: activeTask.status,
                  recovery_required: activeTask.payload.recovery_required ?? false,
                  latest_event: liveEvents.at(-1) ?? null,
                }, null, 2)}</pre>
              </article>
            ) : null}
            {agentInspection ? (
              <>
                <article className="agent-card"><strong>Context 检查器</strong><pre>{JSON.stringify({ session_id: agentInspection.task.payload.session_id, input_chars: agentInspection.task.payload.content.length }, null, 2)}</pre></article>
                <article className="agent-card"><strong>Prompt 检查器</strong><pre>{JSON.stringify(agentDebug?.prompt ?? "任务完成后显示已编译 Prompt 快照", null, 2)}</pre></article>
                <article className="agent-card"><strong>Tool 检查器</strong><pre>{JSON.stringify(agentInspection.events.filter((event) => event.type.startsWith("tool.")), null, 2)}</pre></article>
                <article className="agent-card"><strong>Plan / Approval 检查器</strong><pre>{JSON.stringify({ plan: requirementResult.plan_preview, approvals: agentInspection.approvals }, null, 2)}</pre></article>
                {activeTask?.payload.error ? <article className="agent-card"><strong>失败与恢复策略</strong><pre>{JSON.stringify({ error: activeTask.payload.error, recovery_required: activeTask.payload.recovery_required }, null, 2)}</pre></article> : null}
              </>
            ) : null}
            <h3>字段来源与置信度</h3>
            {Object.entries(requirementResult.requirement.field_evidence).map(([field, value]) => <div className="evidence-row" key={field}><code>{field}</code><span>{value.source_type} · {Math.round(value.confidence * 100)}%</span></div>)}
            <h3>待澄清问题</h3>
            {requirementResult.requirement.open_questions.map((question) => <article className="agent-card" key={question.question_id}><strong>{question.priority} · {question.question}</strong><p>{question.reason}</p><div><button type="button" onClick={() => resolveQuestion(question.question_id, true)}>接受候选</button><button type="button" onClick={() => resolveQuestion(question.question_id, false)}>拒绝候选</button></div></article>)}
            <button type="button" disabled={requirementResult.requirement.status === "confirmed" || requirementResult.requirement.open_questions.length > 0} onClick={() => void confirmRequirement()}>整体确认 Requirement Spec</button>
            <h3>执行计划</h3>
            {requirementResult.plan_preview.map((item) => <article className="plan-node" key={item.node}><strong>{item.node}</strong><span>{item.reason}</span>{item.approval_required ? <em>执行前需审批</em> : null}</article>)}
            {requirementResult.outline ? <><h3>已冻结框架</h3>{requirementResult.outline.sections.map((section) => <article className="plan-node" key={section.title}><strong>{section.title} · {section.target_length}</strong><span>{section.goal}</span></article>)}</> : null}
          </div>
        ) : knowledgeOpen ? (
          <div className="settings-panel knowledge-panel">
            <h2>项目知识库</h2>
            <p>{activeProject ? `当前项目：${activeProject.name}` : "请先创建项目"}</p>
            <label>
              导入资料
              <input
                type="file"
                aria-label="导入知识文件"
                onChange={(event) => setKnowledgeFile(event.target.files?.[0] ?? null)}
              />
            </label>
            <button type="button" onClick={() => void importKnowledge()}>
              导入并建立索引
            </button>
            <label>
              检索问题
              <input
                value={knowledgeQuery}
                onChange={(event) => setKnowledgeQuery(event.target.value)}
              />
            </label>
            <button type="button" onClick={() => void searchKnowledge()}>
              检索知识
            </button>
            <button type="button" onClick={() => void rebuildKnowledge()}>
              重建索引
            </button>
            <h3>已导入文件</h3>
            <div className="project-files">
              {projectFiles.map((file) => (
                <button type="button" key={file.id} onClick={() => void openPreview(file.id)}>
                  <span>{file.original_name}</span>
                  <small>{Math.ceil(file.size_bytes / 1024)} KB</small>
                </button>
              ))}
            </div>
            <div className="knowledge-results" aria-live="polite">
              {knowledgeHits.map((hit) => (
                <article key={hit.item_id} className="knowledge-card">
                  <strong>{hit.title}</strong>
                  <p>{hit.content}</p>
                  <small>
                    可信度：{hit.trust_level} · 引用资格：{hit.citation_policy}
                  </small>
                  <small>保密级别：{hit.confidentiality}</small>
                  <small>检索原因：{hit.retrieval_reason}</small>
                  <small>定位：{JSON.stringify(hit.locator)}</small>
                  {hit.source_uri ? <a href={hit.source_uri}>查看来源</a> : <span>无外部来源</span>}
                  <button type="button" onClick={() => void correctKnowledge(hit.item_id)}>
                    修正为操作手册
                  </button>
                  <button type="button" className="danger" onClick={() => void deleteKnowledge(hit.item_id)}>
                    删除条目
                  </button>
                </article>
              ))}
            </div>
          </div>
        ) : settingsOpen ? (
          <div className="settings-panel">
            <section className="settings-section onboarding-section" aria-labelledby="onboarding-title">
              <div className="settings-section-heading">
                <div>
                  <h2 id="onboarding-title">首次运行与本机能力</h2>
                  <p className="settings-hint">
                    核心功能不要求安装 TeX Live、Typst 或 Pandoc；只有你明确确认后才会启动外部安装。
                  </p>
                </div>
                <span className={`onboarding-state ${firstRun?.completed ? "is-complete" : ""}`}>
                  {firstRun?.completed ? "已完成" : "待确认"}
                </span>
              </div>
              {firstRun ? (
                <>
                  <div className="machine-summary">
                    <span>数据目录：{firstRun.disk.writable ? "可写" : "不可写"}</span>
                    <span>可用磁盘：{(firstRun.disk.free / 1024 ** 3).toFixed(1)} GB</span>
                    <span>GPU：{firstRun.gpu.available ? firstRun.gpu.devices?.join("；") || "可用" : "未检测到 NVIDIA GPU"}</span>
                  </div>
                  <label>
                    可选依赖安装目录（留空使用应用数据目录；TeX Live 建议预留至少 7 GB）
                    <input
                      value={dependencyDestination}
                      onChange={(event) => setDependencyDestination(event.target.value)}
                      placeholder="例如 E:\\App\\PaperAgent-Runtimes\\texlive"
                    />
                  </label>
                  <div className="dependency-list">
                    {firstRun.tools.filter((tool) => ["uv", "typst", "pandoc", "xelatex"].includes(tool.name)).map((tool) => {
                      const job = installJobs[tool.name];
                      return (
                        <article key={tool.name} className="dependency-card">
                          <div>
                            <strong>{tool.name === "xelatex" ? "TeX Live / XeLaTeX" : tool.name}</strong>
                            <small>{tool.purpose} · {tool.optional_size ?? "系统组件"}</small>
                            {job ? <small>安装任务：{job.status}{job.exit_code == null ? "" : ` · exit ${job.exit_code}`}</small> : null}
                          </div>
                          <span className={tool.available ? "dependency-ready" : "dependency-missing"}>
                            {tool.available ? "已就绪" : "未安装"}
                          </span>
                          {!tool.available && !job?.status.match(/running|completed/) ? (
                            <button type="button" onClick={() => void installDependency(tool.name)}>
                              查看方案并安装
                            </button>
                          ) : null}
                        </article>
                      );
                    })}
                  </div>
                  <button type="button" onClick={() => void completeFirstRun()}>
                    {firstRun.completed ? "更新首次运行记录" : "接受当前配置并继续"}
                  </button>
                </>
              ) : <p className="settings-hint">正在读取本机能力…</p>}
            </section>

            <section className="settings-section" aria-labelledby="current-models-title">
              <div className="settings-section-heading">
                <div>
                  <h2 id="current-models-title">当前模型</h2>
                  <p className="settings-hint">切换只影响新任务，正在运行的任务继续使用创建时的 Provider 快照。</p>
                </div>
              </div>
              <div className="current-provider-grid">
                {(["text", "image"] as const).map((modality) => {
                  const current = savedProviders.find((item) => item.modality === modality && item.active && item.enabled);
                  return (
                    <article className="current-provider-card" key={modality}>
                      <span>{modality === "text" ? "文本模型" : "生图模型"}</span>
                      <strong>{current?.display_name || "尚未配置"}</strong>
                      <small>{current ? `${current.model} · ${current.health_status}` : "新增配置后可激活"}</small>
                    </article>
                  );
                })}
              </div>
            </section>

            <section className="settings-section" aria-labelledby="saved-providers-title">
              <div className="settings-section-heading">
                <div>
                  <h2 id="saved-providers-title">已保存 Provider</h2>
                  <p className="settings-hint">API Key 使用 Windows DPAPI 加密，只显示状态，不回填或回显明文。</p>
                </div>
              </div>
              <div className="saved-provider-list">
                {savedProviders.length ? savedProviders.map((item) => (
                  <article className={`saved-provider-card health-${item.health_status}`} key={item.id}>
                    <div className="provider-card-title">
                      <div>
                        <strong>{item.display_name || item.id}</strong>
                        <small>{item.modality === "text" ? "文本" : "生图"} · {item.provider_type} · {item.model}</small>
                      </div>
                      <span className="provider-state">{item.active ? "当前" : item.enabled ? item.health_status : "已停用"}</span>
                    </div>
                    <small className="provider-url">{item.base_url}</small>
                    <small>密钥：{item.credential_status} · 配置 v{item.version} · 密钥 v{item.secret_version}</small>
                    {item.health_detail ? <p className="provider-health-detail">{item.health_detail}</p> : null}
                    {item.enabled ? (
                      <>
                        <div className="provider-actions">
                          <button type="button" disabled={item.active} onClick={() => void activateProvider(item)}>设为当前</button>
                          <button type="button" onClick={() => void testProvider(item)}>测试真实连接</button>
                          <button type="button" className="danger" disabled={item.active} onClick={() => void disableProvider(item)}>停用</button>
                        </div>
                        <div className="provider-key-rotation">
                          <input
                            aria-label={`替换 ${item.id} 的 API Key`}
                            type="password"
                            autoComplete="new-password"
                            value={replacementKeys[item.id] ?? ""}
                            onChange={(event) => setReplacementKeys((current) => ({ ...current, [item.id]: event.target.value }))}
                            placeholder="输入新 Key（不会显示旧 Key）"
                          />
                          <button type="button" onClick={() => void replaceProviderKey(item)}>替换密钥</button>
                        </div>
                      </>
                    ) : null}
                  </article>
                )) : <p className="empty-setting">尚无已保存配置。</p>}
              </div>
            </section>

            <section className="settings-section" aria-labelledby="new-text-provider-title">
              <h2 id="new-text-provider-title">新增文本 Provider</h2>
              <p className="settings-hint">这是独立的空白新增表单，不会载入已保存配置。</p>
              <div className="provider-form-grid">
                <label>
                  Provider 类型
                  <select value={providerType} onChange={(event) => {
                    const type = event.target.value;
                    setProviderType(type);
                    setProviderUrl(PROVIDER_PRESETS[type] ?? PROVIDER_PRESETS.custom_openai);
                  }}>
                    <option value="openai_compatible">OpenAI</option>
                    <option value="anthropic">Claude / Anthropic</option>
                    <option value="gemini">Gemini</option>
                    <option value="deepseek">DeepSeek</option>
                    <option value="doubao">豆包</option>
                    <option value="xiaomi_mimo">Xiaomi MiMo</option>
                    <option value="ollama">Ollama（本地）</option>
                    <option value="custom_openai">自定义 OpenAI-compatible</option>
                  </select>
                </label>
                <label>配置名称<input value={providerId} onChange={(event) => setProviderId(event.target.value)} placeholder="例如 main-writing-model" /></label>
                <label className="provider-form-wide">API URL<input type="url" value={providerUrl} onChange={(event) => setProviderUrl(event.target.value)} placeholder="https://api.example.com/v1" /></label>
                <label>模型名<input value={providerModel} onChange={(event) => setProviderModel(event.target.value)} placeholder="例如 gpt-5、claude-sonnet、mimo-v2" /></label>
                <label>API Key<input type="password" autoComplete="new-password" value={providerKey} onChange={(event) => setProviderKey(event.target.value)} placeholder={providerType === "ollama" ? "本地模型可留空" : "输入 API Key"} /></label>
              </div>
              <button type="button" onClick={() => void saveProvider("text")}>保存文本 Provider</button>
            </section>

            <section className="settings-section" aria-labelledby="new-image-provider-title">
              <h2 id="new-image-provider-title">新增生图 Provider</h2>
              <p className="settings-hint">生图配置与文本模型完全分域；真实生图前仍会单独请求用户确认。</p>
              <div className="provider-form-grid">
                <label>
                  Provider 类型
                  <select value={imageProviderType} onChange={(event) => {
                    const type = event.target.value;
                    setImageProviderType(type);
                    setImageProviderUrl(PROVIDER_PRESETS[type] ?? PROVIDER_PRESETS.custom_image);
                  }}>
                    <option value="seedream_image">Seedream</option>
                    <option value="openai_image">OpenAI Image</option>
                    <option value="custom_image">自定义兼容接口</option>
                  </select>
                </label>
                <label>配置名称<input value={imageProviderId} onChange={(event) => setImageProviderId(event.target.value)} placeholder="例如 illustration-model" /></label>
                <label className="provider-form-wide">API URL<input type="url" value={imageProviderUrl} onChange={(event) => setImageProviderUrl(event.target.value)} placeholder="https://api.example.com/v1" /></label>
                <label>模型名<input value={imageProviderModel} onChange={(event) => setImageProviderModel(event.target.value)} placeholder="例如 seedream-4.0" /></label>
                <label>API Key<input type="password" autoComplete="new-password" value={imageProviderKey} onChange={(event) => setImageProviderKey(event.target.value)} placeholder="输入生图 API Key" /></label>
              </div>
              <button type="button" onClick={() => void saveProvider("image")}>保存生图 Provider</button>
            </section>

            <h2>隐私</h2>
            <label>
              隐私模式
              <select value={privacyMode} onChange={(event) => setPrivacyMode(event.target.value)}>
                <option value="standard">标准</option>
                <option value="privacy-controlled">隐私控制</option>
                <option value="offline">完全离线</option>
              </select>
            </label>
            <button type="button" onClick={() => void savePrivacy()}>保存隐私设置</button>
            <h2>本机资源并发</h2>
            <p className="settings-hint">所有上限均为有限正整数；GPU、实验与项目写入会按队列等待。</p>
            <div className="resource-limit-grid">
              {Object.entries(resourceLimits).map(([name, value]) => (
                <label key={name}>
                  {name}
                  <input
                    type="number"
                    min={1}
                    max={name === "remote_llm" ? 32 : 16}
                    value={value}
                    onChange={(event) => setResourceLimits((current) => ({
                      ...current,
                      [name]: Math.max(1, Number(event.target.value) || 1),
                    }))}
                  />
                </label>
              ))}
            </div>
            <button type="button" onClick={() => void saveResourceLimits()}>
              保存资源上限
            </button>
            <h2>长期记忆</h2>
            <label>
              要记住的偏好
              <textarea value={memoryText} onChange={(event) => setMemoryText(event.target.value)} />
            </label>
            <button type="button" onClick={() => void rememberPreference()}>
              写入记忆
            </button>
            <button type="button" className="danger" onClick={() => void clearMemory()}>
              清空长期记忆
            </button>
          </div>
        ) : previewTabs.length && activeProject && apiToken ? (
          <Suspense fallback={<div className="preview-placeholder">正在加载预览引擎…</div>}>
            {previewTabs.filter((tab) => tab.id === activePreviewTabId).map((tab) => (
              <div className="preview-tab-panel" key={tab.id}>
                <PreviewPane
                  projectId={activeProject.id}
                  token={apiToken}
                  artifact={tab.artifact}
                  parts={tab.parts}
                  viewState={tab.viewState}
                  onViewStateChange={(patch) => updatePreviewViewState(tab.id, patch)}
                  onStatus={setStatus}
                  onLoadMore={() => void loadMorePreview(tab.id)}
                />
              </div>
            ))}
          </Suspense>
        ) : (
          <div className="preview-placeholder">选择文件后在此预览</div>
        )}
      </aside>
    </main>
  );
}
