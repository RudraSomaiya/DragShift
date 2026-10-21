-- Pandoc custom reader for plain text (pandoc has none). The text is kept literal: blank lines
-- split paragraphs and every line break is kept, so "*stars*" or "# hash" stay as typed.
function Reader(input)
  local blocks, lines = {}, {}
  local function flush()
    if #lines > 0 then
      local inlines = pandoc.Inlines {}
      for i, line in ipairs(lines) do
        if i > 1 then inlines:insert(pandoc.LineBreak()) end
        inlines:extend(pandoc.Inlines(line))
      end
      blocks[#blocks + 1] = pandoc.Para(inlines)
      lines = {}
    end
  end
  local text = tostring(input):gsub("\r\n", "\n") .. "\n"
  for line in text:gmatch("(.-)\n") do
    if line:match("^%s*$") then flush() else lines[#lines + 1] = line end
  end
  flush()
  return pandoc.Pandoc(blocks)
end
