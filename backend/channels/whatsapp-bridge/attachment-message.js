'use strict';

const path = require('path');

function attachmentTransportReady({ hasSocket, connectionStatus, messageSendReady }) {
    return Boolean(hasSocket && connectionStatus === 'connected' && messageSendReady);
}

function buildDocumentMessage({ fileBuffer, filePath, caption, mimeType }) {
    return {
        document: fileBuffer,
        mimetype: mimeType || 'application/octet-stream',
        fileName: path.basename(filePath),
        caption: caption || undefined,
    };
}

async function submitAttachment({
    lifecycle, jid, fileBuffer, filePath, caption, mimeType,
    correlationId, sessionId,
}) {
    const content = buildDocumentMessage({ fileBuffer, filePath, caption, mimeType });
    const metadata = {
        outbound_kind: 'attachment',
        attachment_name: content.fileName,
        attachment_mime_type: content.mimetype,
    };
    if (sessionId) metadata.session_id = sessionId;

    const result = await lifecycle.accept({ correlationId, jid, content, metadata });
    const accepted = result.status === 'accepted';
    const body = {
        success: accepted,
        status: result.status,
        correlation_id: correlationId,
        message_id: result.message_id,
        retry_count: result.retry_count,
    };
    return { httpStatus: accepted ? 200 : 500, body };
}

module.exports = { attachmentTransportReady, buildDocumentMessage, submitAttachment };
