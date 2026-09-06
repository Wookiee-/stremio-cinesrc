export function buildEmbedPath({ id, type, season, episode }) {
  // NOTE (stremio-cinesrc): cinesrc.st now serves query-form embeds;
  // path-form (/embed/tv/id/s/e) returns a 404 page.
  if (type === 'tv') return `/embed/tv/${id}?s=${season ?? 1}&e=${episode ?? 1}`
  return `/embed/movie/${id}`
}

function parseEmbedPath(embedPath) {
  const [path, query] = embedPath.split('?')
  const params = new URLSearchParams(query ?? '')
  const match = path.match(/^\/embed\/(movie|tv)\/([^/]+)(?:\/([^/]+))?(?:\/([^/]+))?\/?$/)
  if (!match) throw new Error('Unsupported embed path.')
  const [, kind, id, season, episode] = match
  return {
    kind,
    id: decodeURIComponent(id),
    season: season ?? params.get('s') ?? params.get('season') ?? null,
    episode: episode ?? params.get('e') ?? params.get('episode') ?? null,
  }
}

export function buildProtectionHeader(embedPath) {
  const { kind, id, season, episode } = parseEmbedPath(embedPath)
  return Buffer.from(JSON.stringify([kind, id, season, episode])).toString('base64url')
}

function pageNode() {
  return { children: ['__PAGE__', {}, null, null] }
}

function segment(name, value) {
  return [name, String(value), 'd']
}

function routerNode(segments, leaf) {
  let node = leaf
  for (let i = segments.length - 1; i >= 0; i--) {
    node = { children: [segments[i], node, null, null] }
  }
  return node
}

export function buildRouterStateTree({ id, type, season, episode }) {
  const embedType = type === 'tv' ? 'show' : 'movie'
  const segments =
    type === 'tv'
      ? [segment('type', embedType), segment('id', id), segment('season', season ?? 1), segment('episode', episode ?? 1)]
      : [segment('type', embedType), segment('id', id)]

  const tree = routerNode(segments, pageNode())
  return encodeURIComponent(JSON.stringify(['', { children: ['embed', tree, null, null] }, null, null, true]))
}

export function serializeOptional(value) {
  return value == null ? '$undefined' : value
}
