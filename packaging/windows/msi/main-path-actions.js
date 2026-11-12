var PRODUCT_FOLDER = 'PanasonicPostureManager';

function trimText(value) {
    return String(value == null ? '' : value)
        .replace(/^\s+|\s+$/g, '');
}

function lowerText(value) {
    return String(value).toLowerCase();
}

function pathEquals(left, right) {
    return lowerText(left.replace(/\\+$/, '')) ===
        lowerText(right.replace(/\\+$/, ''));
}

function pathWithin(candidate, parent) {
    var normalizedCandidate = lowerText(candidate.replace(/\\+$/, ''));
    var normalizedParent = lowerText(parent.replace(/\\+$/, ''));
    return normalizedCandidate === normalizedParent ||
        normalizedCandidate.indexOf(normalizedParent + '\\') === 0;
}

function normalizePath(value) {
    var path = trimText(value).replace(/^"+|"+$/g, '').replace(/\//g, '\\');
    if (!/^[A-Za-z]:\\/.test(path)) {
        return null;
    }
    path = path.replace(/\\+/g, '\\').replace(/\\+$/, '');
    if (/^[A-Za-z]:$/.test(path)) {
        path += '\\';
    }
    var drive = path.substring(0, 2);
    var remainder = path.substring(3);
    if (!remainder) {
        return drive + '\\';
    }
    var parts = remainder.split('\\');
    for (var index = 0; index < parts.length; index += 1) {
        var part = parts[index];
        if (!part || part === '.' || part === '..' ||
                /[<>:"|?*\x00-\x1f]/.test(part) ||
                /[. ]$/.test(part)) {
            return null;
        }
    }
    return drive + '\\' + parts.join('\\');
}

function completePath(value) {
    var path = normalizePath(value);
    if (path == null) {
        return null;
    }
    var leaf = path.substring(path.lastIndexOf('\\') + 1);
    if (lowerText(leaf) !== lowerText(PRODUCT_FOLDER)) {
        path = path.replace(/\\+$/, '') + '\\' + PRODUCT_FOLDER;
    }
    return path;
}

function protectedReason(path, windowsFolder, systemFolder, serviceFolder) {
    if ((windowsFolder && pathWithin(path, windowsFolder)) ||
            (systemFolder && pathWithin(path, systemFolder)) ||
            (serviceFolder && pathWithin(path, serviceFolder))) {
        return 'Protected';
    }
    return null;
}

function reparseReason(fileSystem, path) {
    if (!fileSystem) {
        return null;
    }
    var root = fileSystem.GetDriveName(path) + '\\';
    var parts = path.substring(root.length).split('\\');
    var current = root;
    for (var index = 0; index < parts.length; index += 1) {
        if (!parts[index]) {
            continue;
        }
        current += parts[index];
        if (fileSystem.FolderExists(current) &&
                (fileSystem.GetFolder(current).Attributes & 1024)) {
            return 'Reparse';
        }
        current += '\\';
    }
    if (fileSystem.FileExists(path)) {
        return 'Occupied';
    }
    if (fileSystem.FolderExists(path)) {
        var folder = fileSystem.GetFolder(path);
        var target = path.replace(/\\+$/, '');
        var knownInstallation =
            fileSystem.FileExists(target + '\\MANIFEST.json') ||
            (fileSystem.FileExists(target + '\\launch.py') &&
                fileSystem.FolderExists(target + '\\app\\panasonic_posture'));
        if ((folder.Files.Count + folder.SubFolders.Count) > 0 &&
                !knownInstallation) {
            return 'Occupied';
        }
    }
    return null;
}

function validationResult(session, allowCompletion) {
    var raw = session.Property('INSTALLFOLDER');
    var normalizedRaw = normalizePath(raw);
    if (normalizedRaw != null && /^[A-Za-z]:\\$/.test(normalizedRaw)) {
        return { path: normalizedRaw, error: 'Root' };
    }
    var completed = allowCompletion ? completePath(raw) : normalizePath(raw);
    if (completed == null) {
        return { path: raw, error: 'Format' };
    }
    if (completed.length > 180) {
        return { path: completed, error: 'Length' };
    }
    var programFiles = session.Property('ProgramFiles64Folder') ||
        session.Property('ProgramFilesFolder');
    var serviceFolder = programFiles
        ? programFiles.replace(/\\+$/, '') + '\\' + PRODUCT_FOLDER + 'Service'
        : '';
    var protectedCode = protectedReason(
        completed,
        session.Property('WindowsFolder'),
        session.Property('SystemFolder'),
        serviceFolder
    );
    if (protectedCode) {
        return { path: completed, error: protectedCode };
    }
    var fileSystem = new ActiveXObject('Scripting.FileSystemObject');
    var drive = fileSystem.GetDrive(fileSystem.GetDriveName(completed));
    if (drive.DriveType !== 2) {
        return { path: completed, error: 'Network' };
    }
    var filesystemCode = reparseReason(fileSystem, completed);
    if (filesystemCode) {
        return { path: completed, error: filesystemCode };
    }
    return { path: completed, error: null };
}

function applyValidation(session, allowCompletion) {
    var result;
    try {
        result = validationResult(session, allowCompletion);
    } catch (error) {
        result = { path: session.Property('INSTALLFOLDER'), error: 'Format' };
    }
    if (result.error) {
        session.Property('MAIN_PATH_VALID', '');
        session.Property(
            'MAIN_PATH_ERROR',
            session.Property('MainPathError' + result.error) || result.error
        );
        return result;
    }
    session.Property('INSTALLFOLDER', result.path);
    session.Property('MAIN_PATH_ERROR', '');
    session.Property('MAIN_PATH_VALID', '1');
    return result;
}

function CompleteAndValidateMainPath() {
    applyValidation(Session, true);
}

function EnforceMainPath() {
    var result = applyValidation(Session, false);
    if (result.error) {
        throw new Error('Installation path failed validation: ' + result.error);
    }
}

if (typeof WScript !== 'undefined' && WScript.Arguments.length > 0 &&
        WScript.Arguments(0) === '--self-test') {
    if (completePath('D:/Apps') !== 'D:\\Apps\\' + PRODUCT_FOLDER) {
        throw new Error('path completion failed');
    }
    if (completePath('D:\\Apps\\' + PRODUCT_FOLDER + '\\') !==
            'D:\\Apps\\' + PRODUCT_FOLDER) {
        throw new Error('completed path changed');
    }
    if (completePath('relative\\folder') !== null ||
            completePath('\\\\server\\share') !== null ||
            completePath('C:\\bad?folder') !== null) {
        throw new Error('unsafe path syntax was accepted');
    }
    if (protectedReason('C:\\Windows\\PanasonicPostureManager',
            'C:\\Windows', 'C:\\Windows\\System32',
            'C:\\Program Files\\PanasonicPostureManagerService') !== 'Protected') {
        throw new Error('protected path was accepted');
    }
    WScript.Echo('main path actions self-test passed');
}
