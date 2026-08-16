-- Import photos from recovery CSV
-- Reads new_file paths and imports existing files in place.

local LrTasks = import 'LrTasks'
local LrDialogs = import 'LrDialogs'
local LrApplication = import 'LrApplication'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrProgressScope = import 'LrProgressScope'

local catalog = LrApplication.activeCatalog()
local desktop = LrPathUtils.getStandardFilePath("desktop")
local home = LrPathUtils.getStandardFilePath("home")
local COLLECTION_NAME = "new-files-imported"
local OLD_REPLACED_COLLECTION_NAME = "old-entries-replaced"
local OLD_AND_NEW_COLLECTION_NAME = "old-and-new-entries"
local COLLECTION_FLUSH_INTERVAL_SECONDS = 5 * 60
local WRITE_ACCESS_TIMEOUT_SECONDS = 30
local WRITE_ACCESS_MAX_ATTEMPTS = 6
local WRITE_ACCESS_RETRY_SLEEP_SECONDS = 1

local function trim(s)
    return (tostring(s or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

local function csvParseLine(line)
    local out = {}
    local i = 1
    local n = #line

    while i <= n do
        local c = line:sub(i, i)
        if c == '"' then
            i = i + 1
            local field = {}
            while i <= n do
                local ch = line:sub(i, i)
                if ch == '"' then
                    if i < n and line:sub(i + 1, i + 1) == '"' then
                        table.insert(field, '"')
                        i = i + 2
                    else
                        i = i + 1
                        break
                    end
                else
                    table.insert(field, ch)
                    i = i + 1
                end
            end
            table.insert(out, table.concat(field))
            if i <= n and line:sub(i, i) == ',' then
                i = i + 1
            end
        else
            local startPos = i
            while i <= n and line:sub(i, i) ~= ',' do
                i = i + 1
            end
            table.insert(out, line:sub(startPos, i - 1))
            if i <= n and line:sub(i, i) == ',' then
                i = i + 1
            end
        end

        if i > n + 1 then
            break
        end
    end

    if n > 0 and line:sub(n, n) == ',' then
        table.insert(out, "")
    end

    return out
end

local function normalizePath(path)
    local p = trim(path)
    if p == "" then
        return ""
    end

    if p:sub(1, 1) == '"' and p:sub(-1) == '"' and #p >= 2 then
        p = p:sub(2, -2)
    end

    if p:sub(1, 2) == "~/" then
        p = LrPathUtils.child(home, p:sub(3))
    elseif p == "~" then
        p = home
    end

    return trim(p)
end

local function pathExistsAndReadable(path)
    local exists = LrFileUtils.exists(path)
    if exists ~= "file" then
        return false, "path does not exist as a regular file"
    end

    local f, err = io.open(path, "rb")
    if not f then
        return false, "file is not readable: " .. tostring(err or "unknown error")
    end
    f:close()

    return true, nil
end

local function findCollectionInSetRecursive(collectionSet, name)
    local childCollections = collectionSet:getChildCollections()
    if childCollections then
        for _, col in ipairs(childCollections) do
            if col:getName() == name then
                return col
            end
        end
    end

    local childSets = collectionSet:getChildCollectionSets()
    if childSets then
        for _, childSet in ipairs(childSets) do
            local found = findCollectionInSetRecursive(childSet, name)
            if found then
                return found
            end
        end
    end

    return nil
end

local function findExistingCollection(name)
    local rootCollections = catalog:getChildCollections()
    if rootCollections then
        for _, col in ipairs(rootCollections) do
            if col:getName() == name then
                return col
            end
        end
    end

    local rootSets = catalog:getChildCollectionSets()
    if rootSets then
        for _, rootSet in ipairs(rootSets) do
            local found = findCollectionInSetRecursive(rootSet, name)
            if found then
                return found
            end
        end
    end

    return nil
end

local function withCatalogWriteAccess(actionName, func, timeoutSeconds)
    local timeout = timeoutSeconds or WRITE_ACCESS_TIMEOUT_SECONDS
    local maxAttempts = WRITE_ACCESS_MAX_ATTEMPTS

    for attempt = 1, maxAttempts do
        local ok, err
        local status = nil

        if catalog.hasWriteAccess then
            ok, err = LrTasks.pcall(func)
        else
            ok, err = LrTasks.pcall(function()
                status = catalog:withWriteAccessDo(actionName, func, {
                    timeout = timeout,
                })
            end)
        end

        if not ok then
            return false, err
        end

        if status ~= "aborted" then
            return true, nil
        end

        if attempt < maxAttempts then
            LrTasks.sleep(WRITE_ACCESS_RETRY_SLEEP_SECONDS)
        else
            return false, "timed out waiting for catalog write access after " .. tostring(maxAttempts) .. " attempts"
        end
    end

    return false, "timed out waiting for catalog write access"
end

local function findOrCreateCollection(name, logf)
    local existingCollection = findExistingCollection(name)
    if existingCollection then
        return existingCollection
    end

    local createdCollection
    local ok, err = withCatalogWriteAccess("Create collection " .. name, function()
        createdCollection = catalog:createCollection(name, nil, true)
    end)

    if not ok then
        if logf then
            logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tFailed to create collection '" .. name .. "': " .. tostring(err) .. "\n")
        end
        return nil
    end

    return createdCollection
end

local function importPhotosFromRecoveryCsv()
    local selected = LrDialogs.runOpenPanel({
        title = "Select recovery CSV",
        prompt = "Choose CSV",
        canChooseFiles = true,
        canChooseDirectories = false,
        allowsMultipleSelection = false,
        fileTypes = { "csv", "CSV" },
    })

    if not selected or #selected == 0 then
        return
    end

    local csvPath = selected[1]
    local csvFile, openErr = io.open(csvPath, "r")
    if not csvFile then
        LrDialogs.message("CSV read error", "Could not open " .. csvPath .. ": " .. tostring(openErr or "unknown error"))
        return
    end

    local logPath = LrPathUtils.child(desktop, "import_recovery_csv_failures.log")
    local logf, logErr = io.open(logPath, "a")
    if not logf then
        csvFile:close()
        LrDialogs.message("Log write error", "Could not open " .. logPath .. ": " .. tostring(logErr or "unknown error"))
        return
    end

    logf:write("\n=== Import photos from recovery CSV: " .. os.date("%Y-%m-%d %H:%M:%S") .. " ===\n")
    logf:write("CSV: " .. tostring(csvPath) .. "\n")

    local headerLine = csvFile:read("*l")
    if not headerLine then
        csvFile:close()
        logf:close()
        LrDialogs.message("CSV error", "The selected CSV is empty.")
        return
    end

    local headers = csvParseLine(headerLine)
    if headers[1] then
        headers[1] = headers[1]:gsub("^\239\187\191", "")
    end

    local newFileColumnIndex = nil
    local missingFileColumnIndex = nil
    for i, name in ipairs(headers) do
        local trimmedName = trim(name)
        if trimmedName == "new_file" then
            newFileColumnIndex = i
        elseif trimmedName == "missing_file" then
            missingFileColumnIndex = i
        end
    end

    if not newFileColumnIndex then
        csvFile:close()
        logf:close()
        LrDialogs.message("CSV error", "Column 'new_file' was not found in the selected CSV.")
        return
    end

    local rows = {}
    for line in csvFile:lines() do
        table.insert(rows, line)
    end
    csvFile:close()

    local progress = LrProgressScope({
        title = "Importing photos from recovery CSV"
    })
    progress:setCancelable(true)

    local importedPhotos = {}
    local oldEntriesReplaced = {}  -- old catalog photos whose new_file was successfully imported
    local oldAndNewPairs = {}      -- {old=photo, new=photo} pairs for old-and-new-entries collection
    local pendingImportedPhotos = {}
    local pendingOldEntriesReplaced = {}
    local pendingOldAndNewPairs = {}
    local importedCollection = nil
    local oldReplacedCollection = nil
    local oldAndNewCollection = nil
    local lastCollectionFlushAt = os.time()
    local counts = {
        rowsExamined = 0,
        blankPaths = 0,
        imported = 0,
        alreadyPresent = 0,
        missingUnreadable = 0,
        failed = 0,
    }

    local function flushPendingCollections(forceFlush)
        local now = os.time()
        if not forceFlush and (now - lastCollectionFlushAt) < COLLECTION_FLUSH_INTERVAL_SECONDS then
            return
        end

        local flushedSomething = false

        if #pendingImportedPhotos > 0 then
            flushedSomething = true
            if not importedCollection then
                importedCollection = findOrCreateCollection(COLLECTION_NAME, logf)
            end

            if importedCollection then
                local addOk, addErr = withCatalogWriteAccess("Add imported photos to " .. COLLECTION_NAME, function()
                    importedCollection:addPhotos(pendingImportedPhotos)
                end)
                if addOk then
                    pendingImportedPhotos = {}
                else
                    logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tFailed to add photos to collection '" .. COLLECTION_NAME .. "': " .. tostring(addErr) .. "\n")
                end
            else
                logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tCould not find or create collection '" .. COLLECTION_NAME .. "'\n")
            end
        end

        if #pendingOldEntriesReplaced > 0 then
            flushedSomething = true
            if not oldReplacedCollection then
                oldReplacedCollection = findOrCreateCollection(OLD_REPLACED_COLLECTION_NAME, logf)
            end

            if oldReplacedCollection then
                local addOk, addErr = withCatalogWriteAccess("Add old entries to " .. OLD_REPLACED_COLLECTION_NAME, function()
                    oldReplacedCollection:addPhotos(pendingOldEntriesReplaced)
                end)
                if addOk then
                    pendingOldEntriesReplaced = {}
                else
                    logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tFailed to add photos to collection '" .. OLD_REPLACED_COLLECTION_NAME .. "': " .. tostring(addErr) .. "\n")
                end
            else
                logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tCould not find or create collection '" .. OLD_REPLACED_COLLECTION_NAME .. "'\n")
            end
        end

        if #pendingOldAndNewPairs > 0 then
            flushedSomething = true
            if not oldAndNewCollection then
                oldAndNewCollection = findOrCreateCollection(OLD_AND_NEW_COLLECTION_NAME, logf)
            end

            if oldAndNewCollection then
                local combinedPending = {}
                for _, pair in ipairs(pendingOldAndNewPairs) do
                    table.insert(combinedPending, pair.old)
                    table.insert(combinedPending, pair.new)
                end
                local addOk, addErr = withCatalogWriteAccess("Add old and new entries to " .. OLD_AND_NEW_COLLECTION_NAME, function()
                    oldAndNewCollection:addPhotos(combinedPending)
                end)
                if addOk then
                    pendingOldAndNewPairs = {}
                else
                    logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tFailed to add photos to collection '" .. OLD_AND_NEW_COLLECTION_NAME .. "': " .. tostring(addErr) .. "\n")
                end
            else
                logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tCould not find or create collection '" .. OLD_AND_NEW_COLLECTION_NAME .. "'\n")
            end
        end

        if forceFlush or flushedSomething then
            lastCollectionFlushAt = os.time()
        end
    end

    for index, line in ipairs(rows) do
        if progress:isCanceled() then
            break
        end

        counts.rowsExamined = counts.rowsExamined + 1
        progress:setPortionComplete(index - 1, #rows)
        progress:setCaption("Processing row " .. tostring(index) .. " of " .. tostring(#rows))

        local values = csvParseLine(line)
        local rawPath = values[newFileColumnIndex] or ""
        local normalizedPath = normalizePath(rawPath)

        local rawMissingPath = missingFileColumnIndex and (values[missingFileColumnIndex] or "") or ""
        local normalizedMissingPath = normalizePath(rawMissingPath)

        if normalizedPath == "" then
            counts.blankPaths = counts.blankPaths + 1
        else
            local readable, readErr = pathExistsAndReadable(normalizedPath)
            if not readable then
                counts.missingUnreadable = counts.missingUnreadable + 1
                logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t" .. normalizedPath .. "\t" .. tostring(readErr) .. "\n")
            else
                local existingPhoto = catalog:findPhotoByPath(normalizedPath)
                if existingPhoto then
                    counts.alreadyPresent = counts.alreadyPresent + 1
                else
                    local importedPhoto = nil
                    local importErr = nil

                    local ok, writeErr = withCatalogWriteAccess("Import photo from recovery CSV", function()
                        local okAdd, resultOrErr = LrTasks.pcall(function()
                            return catalog:addPhoto(normalizedPath)
                        end)

                        if okAdd then
                            importedPhoto = resultOrErr
                            if not importedPhoto then
                                importErr = "catalog:addPhoto returned nil"
                            end
                        else
                            importErr = resultOrErr
                        end
                    end)

                    if not ok then
                        importErr = writeErr
                    end

                    if importedPhoto then
                        counts.imported = counts.imported + 1
                        table.insert(importedPhotos, importedPhoto)
                        table.insert(pendingImportedPhotos, importedPhoto)

                        if normalizedMissingPath ~= "" then
                            local oldPhoto = catalog:findPhotoByPath(normalizedMissingPath)
                            if oldPhoto then
                                table.insert(oldEntriesReplaced, oldPhoto)
                                table.insert(pendingOldEntriesReplaced, oldPhoto)
                                local pair = { old = oldPhoto, new = importedPhoto }
                                table.insert(oldAndNewPairs, pair)
                                table.insert(pendingOldAndNewPairs, pair)
                            end
                        end
                    else
                        counts.failed = counts.failed + 1
                        logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t" .. normalizedPath .. "\t" .. tostring(importErr or "unknown import failure") .. "\n")
                    end
                end
            end
        end

        flushPendingCollections(false)
        LrTasks.yield()
    end

    flushPendingCollections(true)

    if #importedPhotos > 0 then
        local activePhoto = importedPhotos[1]
        local selectedPhotos = {}
        for i = 2, #importedPhotos do
            table.insert(selectedPhotos, importedPhotos[i])
        end

        local selectOk, selectErr = LrTasks.pcall(function()
            catalog:setSelectedPhotos(activePhoto, selectedPhotos)
        end)

        if not selectOk then
            logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<selection>\tFailed to select imported photos: " .. tostring(selectErr) .. "\n")
        end
    end

    progress:setPortionComplete(#rows, #rows)
    progress:done()

    logf:write("Summary: rowsExamined=" .. tostring(counts.rowsExamined)
        .. ", blankPaths=" .. tostring(counts.blankPaths)
        .. ", imported=" .. tostring(counts.imported)
        .. ", alreadyPresent=" .. tostring(counts.alreadyPresent)
        .. ", missingUnreadable=" .. tostring(counts.missingUnreadable)
        .. ", failed=" .. tostring(counts.failed)
        .. "\n")
    logf:close()

    local completionTitle = progress:isCanceled() and "Import canceled" or "Import complete"
    local completionBody = "Imported: " .. tostring(counts.imported)
        .. "\nAlready present in catalog: " .. tostring(counts.alreadyPresent)
        .. "\nMissing/unreadable: " .. tostring(counts.missingUnreadable)
        .. "\nFailed: " .. tostring(counts.failed)
        .. "\nSkipped blank paths: " .. tostring(counts.blankPaths)
        .. "\n\nFailure log: " .. tostring(logPath)

    if #importedPhotos > 0 then
        completionBody = completionBody .. "\nNew imports collection: " .. COLLECTION_NAME
    end
    if #oldEntriesReplaced > 0 then
        completionBody = completionBody
            .. "\nOld entries replaced collection: " .. OLD_REPLACED_COLLECTION_NAME
            .. " (" .. tostring(#oldEntriesReplaced) .. " entries)"
            .. "\nOld + new side-by-side collection: " .. OLD_AND_NEW_COLLECTION_NAME
            .. " (" .. tostring(#oldAndNewPairs) .. " pairs)"
            .. "\n\nSuggested workflow:"
            .. "\n1. Open the '" .. OLD_AND_NEW_COLLECTION_NAME .. "' collection and sort by filename so old and new entries appear side-by-side."
            .. "\n2. Copy develop settings from the old entry to the new one if needed."
            .. "\n3. Check resolution and other metadata."
            .. "\n4. Flag old entries as Rejected (X key) if the new file is confirmed good."
            .. "\n5. Optionally move the new file in the filesystem to match the old file's folder."
            .. "\n6. To remove old entries from the entire catalog: select them in the collection, then click on a folder in the Folders panel (not the collection) and use Photo > Remove Photo from Catalog. (Deleting while in a collection view only removes the photo from that collection, not from the whole catalog.)"
    end

    LrDialogs.message(completionTitle, completionBody)
end

LrTasks.startAsyncTask(importPhotosFromRecoveryCsv)
