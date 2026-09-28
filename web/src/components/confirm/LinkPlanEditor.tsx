/** LinkPlan 清单编辑器：仅开放 §2.3 修订契约字段。 */

import type { LinkPlan, LinkRef, StoryRef } from '../../api/domain'

type Selection =
  | { kind: 'link'; id: string }
  | { kind: 'story'; id: string }
  | null

type Props = {
  plan: LinkPlan
  selection: Selection
  onSelect: (sel: Selection) => void
  onChange: (plan: LinkPlan) => void
}

function lowConfidence(c: number) {
  return c < 0.5
}

function nextNewLinkId(links: LinkRef[]): string {
  let n = 1
  const ids = new Set(links.map((l) => l.link_id))
  while (ids.has(`new-link-${n}`)) n += 1
  return `new-link-${n}`
}

function nextNewStoryId(stories: StoryRef[]): string {
  let n = 1
  const ids = new Set(stories.map((s) => s.story_id))
  while (ids.has(`new-story-${n}`)) n += 1
  return `new-story-${n}`
}

export function LinkPlanEditor({
  plan,
  selection,
  onSelect,
  onChange,
}: Props) {
  const selectedLink =
    selection?.kind === 'link'
      ? plan.links.find((l) => l.link_id === selection.id)
      : undefined
  const selectedStory =
    selection?.kind === 'story'
      ? plan.stories.find((s) => s.story_id === selection.id)
      : undefined

  function updateLink(linkId: string, patch: Partial<LinkRef>) {
    onChange({
      ...plan,
      links: plan.links.map((l) =>
        l.link_id === linkId ? { ...l, ...patch } : l,
      ),
    })
  }

  function updateStory(storyId: string, patch: Partial<StoryRef>) {
    onChange({
      ...plan,
      stories: plan.stories.map((s) =>
        s.story_id === storyId ? { ...s, ...patch } : s,
      ),
    })
  }

  function deleteLink(linkId: string) {
    onChange({
      ...plan,
      links: plan.links.filter((l) => l.link_id !== linkId),
      stories: plan.stories.filter((s) => s.link_id !== linkId),
    })
    if (selection?.kind === 'link' && selection.id === linkId) onSelect(null)
  }

  function deleteStory(storyId: string) {
    const story = plan.stories.find((s) => s.story_id === storyId)
    onChange({
      ...plan,
      stories: plan.stories.filter((s) => s.story_id !== storyId),
      links: plan.links.map((l) =>
        story && l.link_id === story.link_id
          ? {
              ...l,
              story_ids: l.story_ids.filter((id) => id !== storyId),
            }
          : l,
      ),
    })
    if (selection?.kind === 'story' && selection.id === storyId) onSelect(null)
  }

  function addLink() {
    const linkId = nextNewLinkId(plan.links)
    const storyId = nextNewStoryId(plan.stories)
    const link: LinkRef = {
      link_id: linkId,
      title: '新链路',
      summary: '',
      hit: false,
      entry_id: null,
      entry_version: null,
      confidence: 0,
      story_ids: [storyId],
    }
    const story: StoryRef = {
      story_id: storyId,
      link_id: linkId,
      title: '新用户故事',
      summary: '',
      hit: false,
      entry_id: null,
      entry_version: null,
      confidence: 0,
      rationale: '用户补充',
      related_clause_ids: [],
    }
    onChange({
      ...plan,
      links: [...plan.links, link],
      stories: [...plan.stories, story],
    })
    onSelect({ kind: 'link', id: linkId })
  }

  function bindEntry(
    kind: 'link' | 'story',
    id: string,
    entryId: string,
  ) {
    const trimmed = entryId.trim()
    if (!trimmed) {
      if (kind === 'link') {
        updateLink(id, { entry_id: null, hit: false })
      } else {
        updateStory(id, { entry_id: null, hit: false })
      }
      return
    }
    if (kind === 'link') {
      updateLink(id, { entry_id: trimmed, hit: true })
    } else {
      updateStory(id, { entry_id: trimmed, hit: true })
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <div className="space-y-3" data-testid="link-plan-list">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-stone-800">链路 / 用户故事</h3>
          <button
            type="button"
            data-testid="add-link"
            className="text-xs text-teal-800 underline"
            onClick={addLink}
          >
            补充链路
          </button>
        </div>
        <ul className="space-y-2">
          {plan.links.map((link) => {
            const stories = plan.stories.filter((s) => s.link_id === link.link_id)
            return (
              <li key={link.link_id} className="space-y-1">
                <button
                  type="button"
                  data-testid={`list-item-${link.link_id}`}
                  onClick={() => onSelect({ kind: 'link', id: link.link_id })}
                  className={`flex w-full items-start gap-2 rounded border px-2 py-1.5 text-left text-sm ${
                    selection?.kind === 'link' && selection.id === link.link_id
                      ? 'border-teal-400 bg-teal-50'
                      : 'border-stone-200 bg-white hover:bg-stone-50'
                  } ${lowConfidence(link.confidence) ? 'opacity-60' : ''}`}
                >
                  <span
                    className={`mt-0.5 inline-block h-2 w-2 shrink-0 rounded-full ${
                      link.hit ? 'bg-emerald-500' : 'bg-amber-500'
                    }`}
                    aria-hidden
                  />
                  <span className="min-w-0">
                    <span className="block truncate font-medium text-stone-900">
                      {link.title}
                    </span>
                    <span className="block truncate text-xs text-stone-500">
                      {link.hit ? '命中' : '新增'} · conf{' '}
                      {link.confidence.toFixed(2)}
                    </span>
                  </span>
                </button>
                <ul className="ml-4 space-y-1">
                  {stories.map((story) => (
                    <li key={story.story_id}>
                      <button
                        type="button"
                        data-testid={`list-item-${story.story_id}`}
                        onClick={() =>
                          onSelect({ kind: 'story', id: story.story_id })
                        }
                        className={`flex w-full items-start gap-2 rounded border px-2 py-1 text-left text-xs ${
                          selection?.kind === 'story' &&
                          selection.id === story.story_id
                            ? 'border-teal-400 bg-teal-50'
                            : 'border-stone-100 bg-stone-50/80 hover:bg-stone-100'
                        } ${lowConfidence(story.confidence) ? 'opacity-60' : ''}`}
                      >
                        <span
                          className={`mt-0.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
                            story.hit ? 'bg-emerald-500' : 'bg-amber-500'
                          }`}
                          aria-hidden
                        />
                        <span className="truncate text-stone-800">
                          {story.title}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </li>
            )
          })}
        </ul>
      </div>

      <div
        className="rounded border border-stone-200 bg-white p-3"
        data-testid="link-plan-detail"
      >
        {!selectedLink && !selectedStory ? (
          <p className="text-sm text-stone-500">从左侧选择一条链路或用户故事</p>
        ) : null}

        {selectedLink ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-stone-900">编辑链路</h3>
              <button
                type="button"
                data-testid="delete-item"
                className="text-xs text-red-700 underline"
                onClick={() => deleteLink(selectedLink.link_id)}
              >
                删除
              </button>
            </div>
            <label className="block text-xs text-stone-600">
              标题
              <input
                data-testid="edit-title"
                className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm"
                value={selectedLink.title}
                onChange={(e) =>
                  updateLink(selectedLink.link_id, { title: e.target.value })
                }
              />
            </label>
            <label className="block text-xs text-stone-600">
              摘要
              <textarea
                data-testid="edit-summary"
                className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm"
                rows={3}
                value={selectedLink.summary}
                onChange={(e) =>
                  updateLink(selectedLink.link_id, { summary: e.target.value })
                }
              />
            </label>
            {!selectedLink.hit || !selectedLink.entry_id ? (
              <label className="block text-xs text-stone-600">
                绑定知识库 entry_id（填入后视为命中）
                <input
                  data-testid="edit-entry-id"
                  className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm font-mono"
                  value={selectedLink.entry_id ?? ''}
                  placeholder="例如 business/wiki/xxx.md"
                  onChange={(e) =>
                    bindEntry('link', selectedLink.link_id, e.target.value)
                  }
                />
              </label>
            ) : (
              <p className="text-xs text-stone-500">
                entry_id：{selectedLink.entry_id}
              </p>
            )}
          </div>
        ) : null}

        {selectedStory ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-stone-900">
                编辑用户故事
              </h3>
              <button
                type="button"
                data-testid="delete-item"
                className="text-xs text-red-700 underline"
                onClick={() => deleteStory(selectedStory.story_id)}
              >
                删除
              </button>
            </div>
            <label className="block text-xs text-stone-600">
              标题
              <input
                data-testid="edit-title"
                className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm"
                value={selectedStory.title}
                onChange={(e) =>
                  updateStory(selectedStory.story_id, {
                    title: e.target.value,
                  })
                }
              />
            </label>
            <label className="block text-xs text-stone-600">
              摘要
              <textarea
                data-testid="edit-summary"
                className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm"
                rows={3}
                value={selectedStory.summary}
                onChange={(e) =>
                  updateStory(selectedStory.story_id, {
                    summary: e.target.value,
                  })
                }
              />
            </label>
            <p className="text-xs text-stone-500">
              归属链路：{selectedStory.link_id} · 理由：
              {selectedStory.rationale || '—'}
            </p>
            {!selectedStory.hit || !selectedStory.entry_id ? (
              <label className="block text-xs text-stone-600">
                绑定知识库 entry_id（填入后视为命中）
                <input
                  data-testid="edit-entry-id"
                  className="mt-1 w-full rounded border border-stone-300 px-2 py-1.5 text-sm font-mono"
                  value={selectedStory.entry_id ?? ''}
                  placeholder="例如 business/wiki/xxx.md"
                  onChange={(e) =>
                    bindEntry('story', selectedStory.story_id, e.target.value)
                  }
                />
              </label>
            ) : (
              <p className="text-xs text-stone-500">
                entry_id：{selectedStory.entry_id}
              </p>
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}
