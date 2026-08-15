-- Find Matches to Missing Photos To Possibly Link
-- Lightroom plugin script (main.lua)

local LrTasks = import 'LrTasks'
local LrDialogs = import 'LrDialogs'
local LrApplication = import 'LrApplication'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrDate = import 'LrDate'
local LrProgressScope = import 'LrProgressScope'
local catalog = LrApplication.activeCatalog()
local desktop = LrPathUtils.getStandardFilePath("desktop")

local TIME_DELTA = 5 * 60 -- 5 minutes in seconds

local function logToFile(path, content)
    local f, err = io.open(path, "a")
    if not f then
        LrDialogs.message("Log write error", "Could not open " .. path .. ": " .. (err or "unknown error"))
        return
    end
    f:write(content .. "\n")
    f:close()
end

local function compareTimestamps(t1, t2)
    if not (t1 and t2) then return false end
    local delta = math.abs(LrDate.timeFromIsoDate(t1) - LrDate.timeFromIsoDate(t2))
    return delta <= TIME_DELTA
end

local function isPhotoMissing(photo)
	local path = photo:getRawMetadata("path")
	return path and not LrFileUtils.exists(path)
end

local function isPhotoPresent(photo)
    return not isPhotoMissing(photo)
end

local function findAndCompareMissingPhotos()
    local photos = catalog:getTargetPhotos()
    local totalPhotos = #photos
    local relinkPath = LrPathUtils.child(desktop, "link_missing.sh")
    local ambiguousPath = LrPathUtils.child(desktop, "ambiguous_match.csv")
    local possiblePath = LrPathUtils.child(desktop, "possible_matches.txt")
    local progressScope = LrProgressScope({
        title = "Finding matches for missing photos"
    })

    progressScope:setCancelable(true)

    LrFileUtils.delete(relinkPath)
    LrFileUtils.delete(ambiguousPath)
    LrFileUtils.delete(possiblePath)

    for index, photo in ipairs(photos) do
        if progressScope:isCanceled() then
            break
        end

        local fileName = photo:getFormattedMetadata("fileName") or "Untitled"
        progressScope:setCaption("Checking " .. index .. " of " .. totalPhotos .. ": " .. fileName)
        progressScope:setPortionComplete(index - 1, totalPhotos)

        if isPhotoMissing(photo) then
            local nameWithoutExt = fileName:match("(.+)%..+$") or fileName
            local dateTime = photo:getRawMetadata("dateTimeOriginal")
            local camera = photo:getFormattedMetadata("cameraModel") or ""
            local width = photo:getRawMetadata("width")
            local height = photo:getRawMetadata("height")
            local missingPhotoPath = photo:getRawMetadata("path")

            local candidates = catalog:findPhotos({
                searchDesc = {
                    {
                        criteria = "filename",
                        operation = "contains",
                        value = nameWithoutExt,
                        searchable = true
                    }
                }
            })

            local matches = {}
            local possibles = {}

            for _, candidate in ipairs(candidates) do
                if candidate ~= photo and isPhotoPresent(candidate) then
                    local cTime = candidate:getRawMetadata("dateTimeOriginal")
                    local cCamera = candidate:getFormattedMetadata("cameraModel") or ""
                    local cWidth = candidate:getRawMetadata("width")
                    local cHeight = candidate:getRawMetadata("height")
                    local candidatePath = candidate:getRawMetadata("path")

                    if compareTimestamps(dateTime, cTime) then
                        if cCamera == camera and cWidth == width and cHeight == height then
                            table.insert(matches, candidatePath)
                        else
                            table.insert(possibles, candidatePath)
                        end
                    end
                end
            end

            if #matches == 1 then
                logToFile(relinkPath, "ln '" .. matches[1] .. "' '" .. missingPhotoPath .. "'")
            elseif #matches > 1 then
                logToFile(ambiguousPath, missingPhotoPath .. "," .. table.concat(matches, "; "))
            elseif #possibles > 0 then
                logToFile(possiblePath, missingPhotoPath .. "\n  Possible matches:\n    " .. table.concat(possibles, "\n    "))
            end
        end

        progressScope:setPortionComplete(index, totalPhotos)

        if index % 10 == 0 then
            LrTasks.yield()
        end
    end

    progressScope:done()

    if progressScope:isCanceled() then
        LrDialogs.message("Match search canceled.", "Partial results may have been saved to the Desktop.")
    else
        LrDialogs.message(
            "Match search complete.",
            "Results saved to Desktop:\n"
                .. "- link_missing.sh\n"
                .. "- ambiguous_match.csv\n"
                .. "- possible_matches.txt"
        )
    end
end

LrTasks.startAsyncTask(findAndCompareMissingPhotos)
