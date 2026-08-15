-- Check For Same or Better
-- For selected photos, finds catalog entries (not missing) with the same or better
-- resolution, matching date/time, camera, and filename stem.
-- Adds matching selected photos to a special collection "Has Same Or Better".
-- Intended for photos in a "downloaded-smart-previews" folder that may now have
-- higher-resolution originals (or equivalent copies) elsewhere in the catalog.

local LrTasks = import 'LrTasks'
local LrDialogs = import 'LrDialogs'
local LrApplication = import 'LrApplication'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrProgressScope = import 'LrProgressScope'
local catalog = LrApplication.activeCatalog()
local desktop = LrPathUtils.getStandardFilePath("desktop")

local TIME_DELTA = 5 * 60 -- 5 minutes in seconds
local DEBUG_VERBOSE = true
local DEBUG_MAX_ECHO_PHOTOS = 5
local COLLECTION_NAME = "Has Same Or Better"

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
    end
end

local function compareTimestamps(t1, t2, debugPath)
    if not (t1 and t2) then
        debugLog(debugPath, "Timestamp comparison skipped: missing timestamp. t1=" .. tostring(t1) .. ", t2=" .. tostring(t2))
        return false
    end

    if type(t1) ~= "number" or type(t2) ~= "number" then
        debugLog(debugPath, "Timestamp comparison skipped: timestamps are not numeric. t1=" .. tostring(t1) .. ", t2=" .. tostring(t2))
        return false
    end

    local delta = math.abs(t1 - t2)
    local matched = delta <= TIME_DELTA
    debugLog(debugPath, "Timestamp comparison: delta=" .. tostring(delta) .. " seconds, matched=" .. tostring(matched))
    return matched
end

local function isPhotoPresent(photo, debugPath, label)
    debugLog(debugPath, "Checking availability: " .. tostring(label))
    local available = photo:checkPhotoAvailability()
    debugLog(debugPath, "Availability result for " .. tostring(label) .. ": available=" .. tostring(available))
    return available == true
end

-- Returns true if candidateWidth x candidateHeight is same or better resolution
-- than selectedWidth x selectedHeight. "Better" means at least as many pixels.
local function isSameOrBetterResolution(selWidth, selHeight, candWidth, candHeight, debugPath)
    if not (selWidth and selHeight and candWidth and candHeight) then
        debugLog(debugPath, "Resolution comparison skipped: missing dimension(s). sel=" .. tostring(selWidth) .. "x" .. tostring(selHeight)
            .. ", cand=" .. tostring(candWidth) .. "x" .. tostring(candHeight))
        -- If dimensions are unknown, accept as possible match
        return true
    end
    local selPixels = selWidth * selHeight
    local candPixels = candWidth * candHeight
    local result = candPixels >= selPixels
    debugLog(debugPath, "Resolution comparison: sel=" .. tostring(selWidth) .. "x" .. tostring(selHeight)
        .. " (" .. tostring(selPixels) .. "px), cand=" .. tostring(candWidth) .. "x" .. tostring(candHeight)
        .. " (" .. tostring(candPixels) .. "px), sameOrBetter=" .. tostring(result))
    return result
end

local function findOrCreateCollection(debugPath)
    -- Look for existing collection
    local allCollections = catalog:getChildCollections()
    if allCollections then
        for _, col in ipairs(allCollections) do
            if col:getName() == COLLECTION_NAME then
                debugLog(debugPath, "Found existing collection: " .. COLLECTION_NAME)
                return col
            end
        end
    end

    -- Create it
    local newCollection
    catalog:withWriteAccessDo("Create collection " .. COLLECTION_NAME, function()
        newCollection = catalog:createCollection(COLLECTION_NAME, nil, true)
    end)
    debugLog(debugPath, "Created collection: " .. COLLECTION_NAME)
    return newCollection
end

local function addPhotoToCollection(collection, photo, debugPath)
    catalog:withWriteAccessDo("Add photo to " .. COLLECTION_NAME, function()
        collection:addPhotos({ photo })
    end)
    debugLog(debugPath, "Added photo to collection: " .. COLLECTION_NAME)
end

local function checkForSameOrBetter()
    local photos = catalog:getTargetPhotos()
    local totalPhotos = #photos
    local debugPath = LrPathUtils.child(desktop, "check_same_or_better_debug.log")
    local progressScope = LrProgressScope({
        title = "Checking for same or better photos"
    })

    progressScope:setCancelable(true)

    if DEBUG_VERBOSE then
        LrFileUtils.delete(debugPath)
    end

    debugLog(debugPath, "Started Check For Same or Better. selectedPhotos=" .. tostring(totalPhotos))

    if totalPhotos == 0 then
        progressScope:done()
        LrDialogs.message(
            "No photos selected",
            "Select photos first (e.g. all photos in your downloaded-smart-previews folder), then run this command."
        )
        return
    end

    local collection = findOrCreateCollection(debugPath)
    if not collection then
        progressScope:done()
        LrDialogs.message("Error", "Could not find or create the '" .. COLLECTION_NAME .. "' collection.")
        return
    end

    local addedCount = 0
    local skippedCount = 0

    for index, photo in ipairs(photos) do
        if progressScope:isCanceled() then
            debugLog(debugPath, "Canceled before photo index " .. tostring(index))
            break
        end

        progressScope:setPortionComplete(index - 1, totalPhotos)

        debugLog(debugPath, "---- Photo " .. tostring(index) .. " of " .. tostring(totalPhotos) .. " ----")

        local fileName = photo:getFormattedMetadata("fileName") or "Untitled"
        local photoPath = photo:getRawMetadata("path") or ""
        local photoLabel = fileName .. " [" .. photoPath .. "]"

        progressScope:setCaption("Processing " .. index .. " of " .. totalPhotos .. ": " .. fileName)
        setDebugCaption(progressScope, totalPhotos, "Reading metadata for " .. index .. " of " .. totalPhotos .. ": " .. fileName)

        debugLog(debugPath, "Photo filename=" .. tostring(fileName))
        debugLog(debugPath, "Photo path=" .. tostring(photoPath))

        -- Strip extension to get filename stem
        local nameWithoutExt = fileName:match("(.+)%..+$") or fileName
        local dateTime = photo:getRawMetadata("dateTimeOriginal")
        local camera = photo:getFormattedMetadata("cameraModel") or ""
        local width = photo:getRawMetadata("width")
        local height = photo:getRawMetadata("height")

        debugLog(debugPath, "Search stem=" .. tostring(nameWithoutExt))
        debugLog(debugPath, "Metadata: dateTime=" .. tostring(dateTime)
            .. ", camera=" .. tostring(camera)
            .. ", width=" .. tostring(width)
            .. ", height=" .. tostring(height))

        setDebugCaption(progressScope, totalPhotos, "Searching catalog for filename containing: " .. nameWithoutExt)
        debugLog(debugPath, "Before catalog:findPhotos")

        local candidates = catalog:findPhotos({
            searchDesc = {
                criteria = "filename",
                operation = "contains",
                value = nameWithoutExt,
            }
        })

        debugLog(debugPath, "After catalog:findPhotos. candidateCount=" .. tostring(candidates and #candidates or "nil"))

        if not candidates then
            debugLog(debugPath, "Skipping photo because catalog search returned nil.")
            skippedCount = skippedCount + 1
        else
            local foundSameOrBetter = false

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
                    debugLog(debugPath, "Skipping candidate: same Lightroom photo object as selected photo.")
                elseif not isPhotoPresent(candidate, debugPath, "candidate " .. candidateLabel) then
                    debugLog(debugPath, "Skipping candidate: candidate is not available/present in catalog.")
                else
                    local cTime = candidate:getRawMetadata("dateTimeOriginal")
                    local cCamera = candidate:getFormattedMetadata("cameraModel") or ""
                    local cWidth = candidate:getRawMetadata("width")
                    local cHeight = candidate:getRawMetadata("height")

                    debugLog(debugPath, "Candidate metadata: dateTime=" .. tostring(cTime)
                        .. ", camera=" .. tostring(cCamera)
                        .. ", width=" .. tostring(cWidth)
                        .. ", height=" .. tostring(cHeight))

                    -- Match on: same filename stem, same camera, matching timestamp, same or better resolution
                    local sameCamera = (cCamera == camera)
                    local timeMatch = compareTimestamps(dateTime, cTime, debugPath)
                    local resolutionOk = isSameOrBetterResolution(width, height, cWidth, cHeight, debugPath)

                    debugLog(debugPath, "Match result: sameCamera=" .. tostring(sameCamera)
                        .. ", timeMatch=" .. tostring(timeMatch)
                        .. ", resolutionOk=" .. tostring(resolutionOk))

                    if sameCamera and timeMatch and resolutionOk then
                        debugLog(debugPath, "Found same or better: " .. candidateLabel)
                        foundSameOrBetter = true
                        break
                    end
                end

                LrTasks.yield()
            end

            if foundSameOrBetter then
                debugLog(debugPath, "Adding selected photo to collection: " .. photoLabel)
                addPhotoToCollection(collection, photo, debugPath)
                addedCount = addedCount + 1
            else
                debugLog(debugPath, "No same-or-better found for: " .. photoLabel)
            end
        end

        progressScope:setPortionComplete(index, totalPhotos)
        LrTasks.yield()
    end

    progressScope:done()
    debugLog(debugPath, "Finished. addedCount=" .. tostring(addedCount) .. ", skippedCount=" .. tostring(skippedCount))

    if progressScope:isCanceled() then
        LrDialogs.message(
            "Check canceled.",
            "Partial results added to the '" .. COLLECTION_NAME .. "' collection.\n"
                .. "Added: " .. tostring(addedCount) .. " photos."
        )
    else
        LrDialogs.message(
            "Check complete.",
            "Added " .. tostring(addedCount) .. " photo(s) to the '" .. COLLECTION_NAME .. "' collection.\n"
                .. "These photos have a same-or-better version elsewhere in the catalog.\n\n"
                .. "Debug log: " .. debugPath
        )
    end
end

local function runWithErrorLogging()
    local debugPath = LrPathUtils.child(desktop, "check_same_or_better_debug.log")
    checkForSameOrBetter()
end

LrTasks.startAsyncTask(runWithErrorLogging)
