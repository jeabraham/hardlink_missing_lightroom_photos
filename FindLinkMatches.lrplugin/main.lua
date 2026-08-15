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
local DEBUG_VERBOSE = true
local DEBUG_MAX_ECHO_PHOTOS = 5

local function logToFile(path, content)
    local f, err = io.open(path, "a")
    if not f then
        LrDialogs.message("Log write error", "Could not open " .. path .. ": " .. (err or "unknown error"))
        return
    end
    f:write(content .. "\n")
    f:close()
end

local function debugLog(debugPath, message)
    if DEBUG_VERBOSE then
        logToFile(debugPath, os.date("%Y-%m-%d %H:%M:%S") .. "  " .. tostring(message))
    end
end

local function setDebugCaption(progressScope, totalPhotos, message)
    if DEBUG_VERBOSE and totalPhotos <= DEBUG_MAX_ECHO_PHOTOS then
        progressScope:setCaption(message)
        LrTasks.yield()
    end
end

local function compareTimestamps(t1, t2, debugPath)
    if not (t1 and t2) then
        debugLog(debugPath, "Timestamp comparison skipped: missing timestamp. t1=" .. tostring(t1) .. ", t2=" .. tostring(t2))
        return false
    end

    local ok1, parsed1 = pcall(function()
        return LrDate.timeFromIsoDate(t1)
    end)

    local ok2, parsed2 = pcall(function()
        return LrDate.timeFromIsoDate(t2)
    end)

    if not ok1 or not ok2 or not parsed1 or not parsed2 then
        debugLog(debugPath, "Timestamp parse failed. t1=" .. tostring(t1) .. ", ok1=" .. tostring(ok1) .. ", parsed1=" .. tostring(parsed1)
            .. "; t2=" .. tostring(t2) .. ", ok2=" .. tostring(ok2) .. ", parsed2=" .. tostring(parsed2))
        return false
    end

    local delta = math.abs(parsed1 - parsed2)
    local matched = delta <= TIME_DELTA
    debugLog(debugPath, "Timestamp comparison: delta=" .. tostring(delta) .. " seconds, matched=" .. tostring(matched))
    return matched
end

local function isPhotoPresent(photo, debugPath, label)
    debugLog(debugPath, "Checking availability: " .. tostring(label))
    local ok, available = pcall(function()
        return photo:checkPhotoAvailability()
    end)

    debugLog(debugPath, "Availability result for " .. tostring(label) .. ": ok=" .. tostring(ok) .. ", available=" .. tostring(available))
    return ok and available == true
end

local function isPhotoMissing(photo, debugPath, label)
    return not isPhotoPresent(photo, debugPath, label)
end

local function findAndCompareMissingPhotos()
    local photos = catalog:getTargetPhotos()
    local totalPhotos = #photos
    local relinkPath = LrPathUtils.child(desktop, "link_missing.sh")
    local ambiguousPath = LrPathUtils.child(desktop, "ambiguous_match.csv")
    local possiblePath = LrPathUtils.child(desktop, "possible_matches.txt")
    local debugPath = LrPathUtils.child(desktop, "find_missing_debug.log")
    local progressScope = LrProgressScope({
        title = "Finding matches for missing photos"
    })

    progressScope:setCancelable(true)

    LrFileUtils.delete(relinkPath)
    LrFileUtils.delete(ambiguousPath)
    LrFileUtils.delete(possiblePath)

    if DEBUG_VERBOSE then
        LrFileUtils.delete(debugPath)
    end

    debugLog(debugPath, "Started match search. selectedPhotos=" .. tostring(totalPhotos))

    for index, photo in ipairs(photos) do
        if progressScope:isCanceled() then
            debugLog(debugPath, "Canceled before photo index " .. tostring(index))
            break
        end

        progressScope:setPortionComplete(index - 1, totalPhotos)

        debugLog(debugPath, "---- Photo " .. tostring(index) .. " of " .. tostring(totalPhotos) .. " ----")

        local fileName = photo:getFormattedMetadata("fileName") or "Untitled"
        local missingPhotoPath = photo:getRawMetadata("path") or ""
        local photoLabel = fileName .. " [" .. missingPhotoPath .. "]"

        progressScope:setCaption("Processing " .. index .. " of " .. totalPhotos .. ": " .. fileName)
        setDebugCaption(progressScope, totalPhotos, "Reading metadata for " .. index .. " of " .. totalPhotos .. ": " .. fileName)

        debugLog(debugPath, "Photo filename=" .. tostring(fileName))
        debugLog(debugPath, "Photo path=" .. tostring(missingPhotoPath))

        if isPhotoMissing(photo, debugPath, "selected photo " .. photoLabel) then
            local nameWithoutExt = fileName:match("(.+)%..+$") or fileName
            local dateTime = photo:getRawMetadata("dateTimeOriginal")
            local camera = photo:getFormattedMetadata("cameraModel") or ""
            local width = photo:getRawMetadata("width")
            local height = photo:getRawMetadata("height")

            debugLog(debugPath, "Selected photo is missing.")
            debugLog(debugPath, "Search stem=" .. tostring(nameWithoutExt))
            debugLog(debugPath, "Missing metadata: dateTime=" .. tostring(dateTime)
                .. ", camera=" .. tostring(camera)
                .. ", width=" .. tostring(width)
                .. ", height=" .. tostring(height))

            setDebugCaption(progressScope, totalPhotos, "Searching catalog for filename containing: " .. nameWithoutExt)
            debugLog(debugPath, "Before catalog:findPhotos")

            local okFind, candidates = pcall(function()
                return catalog:findPhotos({
                    searchDesc = {
                        {
                            criteria = "filename",
                            operation = "contains",
                            value = nameWithoutExt,
                            searchable = true
                        }
                    }
                })
            end)

            debugLog(debugPath, "After catalog:findPhotos. ok=" .. tostring(okFind)
                .. ", candidateCount=" .. tostring(candidates and #candidates or "nil"))

            if not okFind or not candidates then
                debugLog(debugPath, "Skipping photo because catalog search failed: " .. tostring(candidates))
            else
                local matches = {}
                local possibles = {}

                setDebugCaption(progressScope, totalPhotos, "Comparing " .. tostring(#candidates) .. " candidates for: " .. fileName)

                for candidateIndex, candidate in ipairs(candidates) do
                    if progressScope:isCanceled() then
                        debugLog(debugPath, "Canceled while comparing candidates.")
                        break
                    end

                    local candidateFileName = candidate:getFormattedMetadata("fileName") or "Untitled"
                    local candidatePath = candidate:getRawMetadata("path") or ""
                    local candidateLabel = candidateFileName .. " [" .. candidatePath .. "]"

                    debugLog(debugPath, "Candidate " .. tostring(candidateIndex) .. " of " .. tostring(#candidates) .. ": " .. candidateLabel)
                    setDebugCaption(progressScope, totalPhotos, "Candidate " .. tostring(candidateIndex) .. " of " .. tostring(#candidates) .. ": " .. candidateFileName)

                    if candidate == photo then
                        debugLog(debugPath, "Skipping candidate: same Lightroom photo object as selected missing photo.")
                    elseif not isPhotoPresent(candidate, debugPath, "candidate " .. candidateLabel) then
                        debugLog(debugPath, "Skipping candidate: candidate is not available.")
                    else
                        local cTime = candidate:getRawMetadata("dateTimeOriginal")
                        local cCamera = candidate:getFormattedMetadata("cameraModel") or ""
                        local cWidth = candidate:getRawMetadata("width")
                        local cHeight = candidate:getRawMetadata("height")

                        debugLog(debugPath, "Candidate metadata: dateTime=" .. tostring(cTime)
                            .. ", camera=" .. tostring(cCamera)
                            .. ", width=" .. tostring(cWidth)
                            .. ", height=" .. tostring(cHeight))

                        if compareTimestamps(dateTime, cTime, debugPath) then
                            debugLog(debugPath, "Candidate has timestamp match.")

                            if cCamera == camera and cWidth == width and cHeight == height then
                                debugLog(debugPath, "Candidate is strong match.")
                                table.insert(matches, candidatePath)
                            else
                                debugLog(debugPath, "Candidate is possible match. cameraMatch=" .. tostring(cCamera == camera)
                                    .. ", widthMatch=" .. tostring(cWidth == width)
                                    .. ", heightMatch=" .. tostring(cHeight == height))
                                table.insert(possibles, candidatePath)
                            end
                        else
                            debugLog(debugPath, "Candidate rejected: timestamp did not match.")
                        end
                    end

                    if DEBUG_VERBOSE then
                        LrTasks.yield()
                    end
                end

                debugLog(debugPath, "Photo result: strongMatches=" .. tostring(#matches) .. ", possibleMatches=" .. tostring(#possibles))

                if #matches == 1 then
                    debugLog(debugPath, "Writing relink command.")
                    logToFile(relinkPath, "ln '" .. matches[1] .. "' '" .. missingPhotoPath .. "'")
                elseif #matches > 1 then
                    debugLog(debugPath, "Writing ambiguous match.")
                    logToFile(ambiguousPath, missingPhotoPath .. "," .. table.concat(matches, "; "))
                elseif #possibles > 0 then
                    debugLog(debugPath, "Writing possible matches.")
                    logToFile(possiblePath, missingPhotoPath .. "\n  Possible matches:\n    " .. table.concat(possibles, "\n    "))
                else
                    debugLog(debugPath, "No matches found for selected photo.")
                end
            end
        else
            debugLog(debugPath, "Selected photo is not missing; skipping.")
        end

        progressScope:setPortionComplete(index, totalPhotos)
        LrTasks.yield()
    end

    progressScope:done()
    debugLog(debugPath, "Finished match search.")

    if progressScope:isCanceled() then
        LrDialogs.message("Match search canceled.", "Partial results may have been saved to the Desktop.")
    else
        LrDialogs.message(
            "Match search complete.",
            "Results saved to Desktop:\n"
                .. "- link_missing.sh\n"
                .. "- ambiguous_match.csv\n"
                .. "- possible_matches.txt\n"
                .. "- find_missing_debug.log"
        )
    end
end

local function runWithErrorLogging()
    local debugPath = LrPathUtils.child(desktop, "find_missing_debug.log")

    local ok, err = xpcall(function()
        findAndCompareMissingPhotos()
    end, function(errorMessage)
        return tostring(errorMessage) .. "\n" .. tostring(debug.traceback())
    end)

    if not ok then
        debugLog(debugPath, "FATAL ERROR: " .. tostring(err))
        LrDialogs.message(
            "Match search failed.",
            "A Lua error occurred. Details were written to:\n" .. debugPath .. "\n\n" .. tostring(err)
        )
    end
end

LrTasks.startAsyncTask(runWithErrorLogging)
