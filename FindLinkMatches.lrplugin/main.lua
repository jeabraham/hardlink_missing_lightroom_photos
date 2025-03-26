-- Find Matches to Missing Photos To Possibly Link
-- Lightroom plugin script (main.lua)

local LrTasks = import 'LrTasks'
local LrDialogs = import 'LrDialogs'
local LrApplication = import 'LrApplication'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrDate = import 'LrDate'
local catalog = LrApplication.activeCatalog()
local desktop = LrPathUtils.getStandardFilePath("desktop")

local TIME_DELTA = 5 * 60 -- 5 minutes in seconds

local function logToFile(path, content)
    local f = io.open(path, "a")
    f:write(content .. "\n")
    f:close()
end

local function compareTimestamps(t1, t2)
    if not (t1 and t2) then return false end
    local delta = math.abs(LrDate.timeFromIsoDate(t1) - LrDate.timeFromIsoDate(t2))
    return delta <= TIME_DELTA
end

local function findAndCompareMissingPhotos()
    local photos = catalog:getTargetPhotos()
    local relinkPath = LrPathUtils.child(desktop, "link_missing.sh")
    local ambiguousPath = LrPathUtils.child(desktop, "ambiguous_match.csv")
    local possiblePath = LrPathUtils.child(desktop, "possible_matches.txt")

    LrFileUtils.delete(relinkPath)
    LrFileUtils.delete(ambiguousPath)
    LrFileUtils.delete(possiblePath)

    for _, photo in ipairs(photos) do
        if not photo:isMissing() then
            -- skip non-missing photos
            goto continue
        end

        local fileName = photo:getFormattedMetadata("fileName")
        local nameWithoutExt = fileName:match("(.+)%..+$")
        local dateTime = photo:getRawMetadata("dateTimeOriginal")
        local camera = photo:getFormattedMetadata("cameraModel") or ""
        local width = photo:getRawMetadata("width")
        local height = photo:getRawMetadata("height")

        local candidates = catalog:findPhotos({searchDesc = { {criteria="filename", operation="contains", value=nameWithoutExt, searchable=true} }})
        local matches = {}
        local possibles = {}

        for _, candidate in ipairs(candidates) do
            if candidate == photo or candidate:isMissing() then goto inner_continue end

            local cTime = candidate:getRawMetadata("dateTimeOriginal")
            local cCamera = candidate:getFormattedMetadata("cameraModel") or ""
            local cWidth = candidate:getRawMetadata("width")
            local cHeight = candidate:getRawMetadata("height")

            if compareTimestamps(dateTime, cTime) then
                if cCamera == camera and cWidth == width and cHeight == height then
                    table.insert(matches, candidate:getRawMetadata("path"))
                else
                    table.insert(possibles, candidate:getRawMetadata("path"))
                end
            end

            ::inner_continue::
        end

        if #matches == 1 then
            logToFile(relinkPath, "ln '" .. matches[1] .. "' '" .. photo:getRawMetadata("path") .. "'")
        elseif #matches > 1 then
            logToFile(ambiguousPath, photo:getRawMetadata("path") .. "," .. table.concat(matches, "; "))
        elseif #possibles > 0 then
            logToFile(possiblePath, photo:getRawMetadata("path") .. "\n  Possible matches:\n    " .. table.concat(possibles, "\n    "))
        end

        ::continue::
    end

    LrDialogs.message("Match search complete.", "Results saved to Desktop:
- link_missing.sh
- ambiguous_match.csv
- possible_matches.txt")
end

LrTasks.startAsyncTask(findAndCompareMissingPhotos)
