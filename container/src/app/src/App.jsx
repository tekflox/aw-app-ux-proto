import { useEffect, useState, useCallback } from 'react'

// No router dependency — just two views (dashboard grid, project frame),
// switched on window.location.pathname + history.pushState.
function useRoute() {
  const [path, setPath] = useState(window.location.pathname)
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname)
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  const navigate = useCallback((to) => {
    window.history.pushState({}, '', to)
    setPath(to)
  }, [])
  return [path, navigate]
}

export default function App() {
  const [path, navigate] = useRoute()
  const match = path.match(/^\/p\/([^/]+)\/?$/)
  if (match) {
    return <ProjectFrame slug={match[1]} onBack={() => navigate('/')} />
  }
  return <Dashboard onOpen={(slug) => navigate(`/p/${slug}`)} />
}

function ProjectFrame({ slug, onBack }) {
  // Real isolation (Architect finding #2 on Kanban 39e5bf3b...): this MUST
  // be an absolute URL on the project's own subdomain
  // (ux-proto--<slug>.app.{AW_DOMAIN}, routed by caddy_template.py's
  // wildcard_children matcher straight to this same app). A relative path
  // on the shell's own origin would make allow-same-origin below mean
  // "same origin as the DASHBOARD" — a broken prototype could then reach
  // window.parent/DOM/localStorage/cookies of the shell itself. Flipping
  // the src to absolute and adding allow-same-origin only happen together;
  // separately either one is wrong (opaque + same-origin-with-shell, or a
  // same-origin flag that isn't actually enforcing project isolation).
  // location.hostname is ux-proto.app.{AW_DOMAIN}; dropping the leading
  // "ux-proto" label leaves "app.{AW_DOMAIN}" already — don't prepend
  // another "app." or it double-counts (ux-proto--x.app.app.{domain}).
  const origin = `${window.location.protocol}//ux-proto--${slug}.${window.location.hostname.split('.').slice(1).join('.')}`
  const [snapshots, setSnapshots] = useState([])
  const [version, setVersion] = useState(null) // null = not loaded yet, don't render the iframe with a guessed src
  const [saving, setSaving] = useState(false)

  const refreshSnapshots = useCallback(() => {
    fetch(`/api/projects/${slug}/snapshots`)
      .then((r) => r.json())
      .then((d) => setSnapshots(d.snapshots || []))
      .catch(() => {})
  }, [slug])

  useEffect(() => {
    refreshSnapshots()
    // selected_version is persisted server-side (set_selected_version) —
    // load it as the initial picker value so a page reload / new dashboard
    // session shows whatever was last selected, not always "latest".
    fetch(`/api/projects/${slug}`)
      .then((r) => r.json())
      .then((d) => setVersion(d.selected_version || 'latest'))
      .catch(() => setVersion('latest'))
  }, [slug, refreshSnapshots])

  function selectVersion(v) {
    setVersion(v)
    // Persisted, not just local iframe state — this is what makes the
    // external public_url follow the picker instead of always meaning
    // "latest" (Frederico: "quando ele trocar, ele também troca na URL
    // principal, se eu abrir externo, eu estou assistindo a versão do
    // snapshot selecionada").
    fetch(`/api/projects/${slug}/selected_version`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: v }),
    }).catch(() => {})
  }

  async function saveSnapshot() {
    setSaving(true)
    try {
      const res = await fetch(`/api/projects/${slug}/snapshots`, { method: 'POST' })
      const data = await res.json()
      refreshSnapshots()
      if (data.version) selectVersion(String(data.version))
    } catch (e) {}
    setSaving(false)
  }

  if (version === null) return <div className="h-screen bg-black" />

  const frameSrc = version === 'latest' ? `${origin}/_frame/` : `${origin}/_frame/v/${version}/`

  return (
    <div className="h-screen flex flex-col bg-black">
      <div className="flex items-center gap-3 px-4 py-2 border-b border-white/10 bg-neutral-900">
        <button
          onClick={onBack}
          className="text-sm text-neutral-300 hover:text-white transition-colors"
        >
          ← Projects
        </button>
        <span className="text-sm text-neutral-500">/</span>
        <span className="text-sm font-medium">{slug}</span>
        <div className="ml-auto flex items-center gap-2">
          <select
            value={version}
            onChange={(e) => selectVersion(e.target.value)}
            className="bg-neutral-800 border border-white/10 rounded-md text-xs px-2 py-1 text-neutral-300"
          >
            <option value="latest">Latest (live)</option>
            {snapshots.map((s) => (
              <option key={s.version} value={s.version}>
                Snapshot {s.version} — {new Date(s.created_at * 1000).toLocaleString()}
              </option>
            ))}
          </select>
          <button
            onClick={saveSnapshot}
            disabled={saving}
            className="text-xs px-2.5 py-1 rounded-md bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium"
          >
            {saving ? 'Saving…' : '📸 Save Snapshot'}
          </button>
        </div>
      </div>
      <iframe
        title={slug}
        src={frameSrc}
        sandbox="allow-scripts allow-same-origin"
        className="flex-1 w-full border-0 bg-white"
      />
    </div>
  )
}

function Dashboard({ onOpen }) {
  const [projects, setProjects] = useState([])
  const [includeDeleted, setIncludeDeleted] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [error, setError] = useState(null)

  // Template picker — the human only ever visualizes/picks here; templates
  // and their versions are created exclusively by the piloting agent via MCP
  // (promote_to_template). 'blank' is the sentinel for today's empty scaffold.
  const [templates, setTemplates] = useState([])
  const [templateVersions, setTemplateVersions] = useState({}) // slug -> [{version_label, created_at}]
  const [selectedTemplate, setSelectedTemplate] = useState('blank')
  const [selectedVersion, setSelectedVersion] = useState({}) // slug -> version_label

  const refresh = useCallback(() => {
    fetch(`/api/projects?include_deleted=${includeDeleted}`)
      .then((r) => r.json())
      .then((d) => setProjects(d.projects || []))
      .catch(() => {})
  }, [includeDeleted])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 4000)
    return () => clearInterval(id)
  }, [refresh])

  useEffect(() => {
    if (!creating) return
    fetch('/api/templates')
      .then((r) => r.json())
      .then((d) => {
        const list = d.templates || []
        setTemplates(list)
        list.forEach((t) => {
          fetch(`/api/templates/${t.slug}/versions`)
            .then((r) => r.json())
            .then((vd) => {
              const versions = vd.versions || []
              setTemplateVersions((prev) => ({ ...prev, [t.slug]: versions }))
              // Newest version pre-selected — any earlier one can still be picked.
              if (versions.length) {
                setSelectedVersion((prev) => (prev[t.slug] ? prev : { ...prev, [t.slug]: versions[0].version_label }))
              }
            })
            .catch(() => {})
        })
      })
      .catch(() => {})
  }, [creating])

  async function createProject(e) {
    e.preventDefault()
    if (!newName.trim()) return
    setError(null)
    const body = { name: newName.trim() }
    if (selectedTemplate !== 'blank') {
      body.template_slug = selectedTemplate
      body.version_label = selectedVersion[selectedTemplate]
    }
    const res = await fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      setError('Error creating project')
      return
    }
    setNewName('')
    setCreating(false)
    setSelectedTemplate('blank')
    refresh()
  }

  async function toggleDelete(p) {
    const path = p.status === 'active' ? `/api/projects/${p.slug}` : `/api/projects/${p.slug}/restore`
    const method = p.status === 'active' ? 'DELETE' : 'POST'
    await fetch(path, { method })
    refresh()
  }

  return (
    <div className="min-h-screen bg-gradient-to-b from-neutral-950 to-black text-neutral-100 p-8">
      <div className="max-w-5xl mx-auto">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">UX-Proto</h1>
            <p className="text-sm text-neutral-500 mt-1">
              AI-piloted visual prototyping — real code per project, live hot-reload.
            </p>
          </div>
          <button
            onClick={() => setCreating(true)}
            className="px-4 py-2 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 hover:from-indigo-400 hover:to-violet-500 transition-all text-sm font-medium shadow-lg shadow-indigo-950/50"
          >
            + New Project
          </button>
        </div>

        {creating && (
          <form
            onSubmit={createProject}
            className="mb-6 bg-neutral-900/80 border border-white/10 rounded-lg p-4"
          >
            <label className="block text-xs font-semibold text-neutral-500 mb-1.5">Nome do projeto</label>
            <input
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="ex: checkout-flow-v3"
              className="w-full bg-neutral-950 border border-white/10 rounded-md px-3 py-2 text-sm outline-none placeholder:text-neutral-600 mb-4"
            />

            <label className="block text-xs font-semibold text-neutral-500 mb-2">Começar a partir de</label>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-4">
              <button
                type="button"
                onClick={() => setSelectedTemplate('blank')}
                className={`text-left rounded-lg p-3 border transition-colors ${
                  selectedTemplate === 'blank'
                    ? 'border-indigo-500 bg-indigo-500/10'
                    : 'border-white/10 bg-neutral-950 hover:border-white/20'
                }`}
              >
                <div className="text-lg mb-1">⬜</div>
                <div className="text-xs font-semibold">Blank</div>
                <div className="text-[11px] text-neutral-500">Scaffold vazio</div>
              </button>
              {templates.map((t) => {
                const versions = templateVersions[t.slug] || []
                const selected = selectedTemplate === t.slug
                return (
                  <div
                    key={t.slug}
                    onClick={() => setSelectedTemplate(t.slug)}
                    className={`cursor-pointer text-left rounded-lg p-3 border transition-colors ${
                      selected ? 'border-indigo-500 bg-indigo-500/10' : 'border-white/10 bg-neutral-950 hover:border-white/20'
                    }`}
                  >
                    <div className="text-lg mb-1">📄</div>
                    <div className="text-xs font-semibold">{t.name}</div>
                    <div className="text-[11px] text-neutral-500">{t.description || t.slug}</div>
                    {versions.length > 0 && (
                      <div className="mt-2 pt-2 border-t border-white/10 flex items-center justify-between gap-2">
                        <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-indigo-500/15 text-indigo-300 font-bold">
                          {versions[0].version_label}
                        </span>
                        <select
                          value={selectedVersion[t.slug] || versions[0].version_label}
                          onClick={(e) => e.stopPropagation()}
                          onChange={(e) => {
                            setSelectedTemplate(t.slug)
                            setSelectedVersion((prev) => ({ ...prev, [t.slug]: e.target.value }))
                          }}
                          className="bg-neutral-800 border border-white/10 rounded text-[11px] px-1.5 py-0.5"
                        >
                          {versions.map((v) => (
                            <option key={v.version_label} value={v.version_label}>{v.version_label}</option>
                          ))}
                        </select>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
            <p className="text-[11px] text-neutral-600 italic mb-4">
              O projeto grava a versão exata usada — versões futuras do template nunca afetam projetos já criados.
            </p>

            <div className="flex items-center gap-2">
              <button type="submit" className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 hover:bg-indigo-500">
                Criar projeto
              </button>
              <button
                type="button"
                onClick={() => { setCreating(false); setNewName(''); setSelectedTemplate('blank') }}
                className="text-sm px-3 py-1.5 rounded-md text-neutral-400 hover:text-white"
              >
                Cancel
              </button>
            </div>
          </form>
        )}
        {error && <p className="text-sm text-red-400 mb-4">{error}</p>}

        <label className="flex items-center gap-2 text-xs text-neutral-500 mb-4 cursor-pointer w-fit">
          <input
            type="checkbox"
            checked={includeDeleted}
            onChange={(e) => setIncludeDeleted(e.target.checked)}
            className="accent-indigo-500"
          />
          show deleted
        </label>

        {projects.length === 0 && (
          <div className="text-center text-neutral-600 py-20 text-sm">
            No projects yet. Create one with the button above, or ask the agent via MCP (create_project).
          </div>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {projects.map((p) => (
            <div
              key={p.id}
              className="group rounded-xl p-4 border border-white/10 bg-gradient-to-br from-neutral-900 to-neutral-950 hover:border-indigo-500/50 hover:shadow-lg hover:shadow-indigo-950/30 transition-all"
            >
              <div className="flex items-start justify-between">
                <button
                  onClick={() => onOpen(p.slug)}
                  disabled={p.status !== 'active'}
                  className="text-left font-medium text-neutral-100 group-hover:text-indigo-300 transition-colors disabled:text-neutral-500 disabled:cursor-not-allowed"
                >
                  {p.name}
                </button>
                <span
                  title={
                    p.connected > 0
                      ? `${p.connected} browser${p.connected > 1 ? 's' : ''} currently viewing this project`
                      : 'No browser currently has this project open'
                  }
                  className={`inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-full ${
                    p.connected > 0 ? 'bg-emerald-500/15 text-emerald-400' : 'bg-neutral-800 text-neutral-500'
                  }`}
                >
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${p.connected > 0 ? 'bg-emerald-400' : 'bg-neutral-600'}`}
                  />
                  {p.connected > 0 ? `${p.connected} viewing` : 'not open'}
                </span>
              </div>
              <a
                href={p.public_url}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="block text-xs text-indigo-400/80 hover:text-indigo-300 hover:underline mt-1 truncate"
              >
                {p.public_url}
              </a>
              <div className="flex items-center justify-between mt-4">
                <span className="text-[11px] text-neutral-600">
                  {p.status === 'deleted' ? 'deleted' : `edited ${new Date(p.updated_at).toLocaleString()}`}
                </span>
                <button
                  onClick={() => toggleDelete(p)}
                  className={
                    p.status === 'active'
                      ? 'text-xs px-2.5 py-1 rounded-md border border-red-500/30 text-red-400 hover:bg-red-500/10 hover:border-red-500/50 transition-colors'
                      : 'text-xs px-2.5 py-1 rounded-md border border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/10 hover:border-emerald-500/50 transition-colors'
                  }
                >
                  {p.status === 'active' ? 'Delete' : 'Restore'}
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
