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

local function findOrCreateCollection(logf)
    local allCollections = catalog:getChildCollections()
    if allCollections then
        for _, col in ipairs(allCollections) do
            if col:getName() == COLLECTION_NAME then
                return col
            end
        end
    end

    local createdCollection
    local ok, err = pcall(function()
        catalog:withWriteAccessDo("Create collection " .. COLLECTION_NAME, function()
            createdCollection = catalog:createCollection(COLLECTION_NAME, nil, true)
        end)
    end)

    if not ok then
        if logf then
            logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tFailed to create collection: " .. tostring(err) .. "\n")
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
    for i, name in ipairs(headers) do
        if trim(name) == "new_file" then
            newFileColumnIndex = i
            break
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
    local counts = {
        rowsExamined = 0,
        blankPaths = 0,
        imported = 0,
        alreadyPresent = 0,
        missingUnreadable = 0,
        failed = 0,
    }

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

                    local ok, writeErr = pcall(function()
                        catalog:withWriteAccessDo("Import photo from recovery CSV", function()
                            local okAdd, resultOrErr = pcall(function()
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
                    end)

                    if not ok then
                        importErr = writeErr
                    end

                    if importedPhoto then
                        counts.imported = counts.imported + 1
                        table.insert(importedPhotos, importedPhoto)
                    else
                        counts.failed = counts.failed + 1
                        logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t" .. normalizedPath .. "\t" .. tostring(importErr or "unknown import failure") .. "\n")
                    end
                end
            end
        end

        LrTasks.yield()
    end

    local collection = nil
    if #importedPhotos > 0 then
        collection = findOrCreateCollection(logf)
        if collection then
            local addOk, addErr = pcall(function()
                catalog:withWriteAccessDo("Add imported photos to " .. COLLECTION_NAME, function()
                    collection:addPhotos(importedPhotos)
                end)
            end)
            if not addOk then
                logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tFailed to add photos to collection: " .. tostring(addErr) .. "\n")
            end
        else
            logf:write(os.date("%Y-%m-%d %H:%M:%S") .. "\t<collection>\tCould not find or create collection '" .. COLLECTION_NAME .. "'\n")
        end

        local activePhoto = importedPhotos[1]
        local selectedPhotos = {}
        for i = 2, #importedPhotos do
            table.insert(selectedPhotos, importedPhotos[i])
        end

        local selectOk, selectErr = pcall(function()
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

    if collection then
        completionBody = completionBody .. "\nCollection used: " .. COLLECTION_NAME
    end

    LrDialogs.message(completionTitle, completionBody)
end

LrTasks.startAsyncTask(importPhotosFromRecoveryCsv)
