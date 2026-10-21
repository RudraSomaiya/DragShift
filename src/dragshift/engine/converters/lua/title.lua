-- Pandoc filter: a fallback title from -M dragshift-title. EPUB gets it as its title; HTML as
-- its <title> only (pagetitle), so no title heading is added. A document's own title wins.
function Meta(meta)
  local fallback = meta["dragshift-title"]
  meta["dragshift-title"] = nil
  if fallback ~= nil and meta.title == nil then
    if FORMAT:match("epub") then
      meta.title = fallback
    elseif meta.pagetitle == nil then
      meta.pagetitle = fallback
    end
  end
  return meta
end
