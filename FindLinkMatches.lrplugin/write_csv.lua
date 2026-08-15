-- Write CSV File for Photos
-- Exports selected missing photos (and required metadata) for relink_missing_photos.py

local LrTasks = import 'LrTasks'
local LrDialogs = import 'LrDialogs'
local LrApplication = import 'LrApplication'
local LrPathUtils = import 'LrPathUtils'
local LrDate = import 'LrDate'

local catalog = LrApplication.activeCatalog()
local desktop = LrPathUtils.getStandardFilePath("desktop")

local function csvEscape(value)
    value = tostring(value or "")
    value = value:gsub('"', '""')
    return '"' .. value .. '"'
end

local function writeCsvFileForPhotos()
    local photos = catalog:getTargetPhotos()
    if #photos == 0 then
        LrDialogs.message(
            "No photos selected",
            "Select photos first (typically the Missing Photographs collection), then run this command."
        )
        return
    end

    local outputPath = LrPathUtils.child(desktop, "Missing_Photos.csv")
    local f, err = io.open(outputPath, "w")
    if not f then
        LrDialogs.message("CSV write error", "Could not open " .. outputPath .. ": " .. (err or "unknown error"))
        return
    end

    local columns = {
        "Photo",
        "Filename",
        "Date/Time Original (Capture)",
        "Width",
        "Height",
        "Camera Make",
    }
    f:write(table.concat(columns, ",") .. "\n")

    local exported = 0

    for _, photo in ipairs(photos) do
        local path = photo:getRawMetadata("path") or ""
        local fileName = photo:getFormattedMetadata("fileName") or ""
        local rawDate = photo:getRawMetadata("dateTimeOriginal")
        local captureDate = (rawDate and type(rawDate) == "number") and LrDate.timeToUserFormat(rawDate, "%Y-%m-%d %H:%M:%S") or tostring(rawDate or "")
        local width = photo:getRawMetadata("width") or ""
        local height = photo:getRawMetadata("height") or ""
        local cameraMake = photo:getFormattedMetadata("cameraMake")
        if not cameraMake or cameraMake == "" then
            cameraMake = photo:getFormattedMetadata("cameraModel") or ""
        end

        local row = {
            csvEscape(path),
            csvEscape(fileName),
            csvEscape(captureDate),
            csvEscape(width),
            csvEscape(height),
            csvEscape(cameraMake),
        }
        f:write(table.concat(row, ",") .. "\n")
        exported = exported + 1
    end

    f:close()

    LrDialogs.message(
        "CSV export complete",
        "Wrote " .. exported .. " missing-photo rows to:\n" .. outputPath
            .. "\n\nColumns: Photo, URL, Filename, Date/Time Original (Capture), Width, Height, Camera Make"
    )
end

LrTasks.startAsyncTask(writeCsvFileForPhotos)
