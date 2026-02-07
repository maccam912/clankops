#!/usr/bin/env node
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const cliPath = (process.env.ATPROTO_MCP_CLI_PATH || '').trim();
if (!cliPath) {
  console.error('Error: ATPROTO_MCP_CLI_PATH is not set (internal wrapper error).');
  process.exit(1);
}
if (!fs.existsSync(cliPath)) {
  console.error(`Error: ATPROTO_MCP_CLI_PATH does not exist: ${cliPath}`);
  process.exit(1);
}

const cliUrl = pathToFileURL(cliPath).href;
const distDirUrl = new URL('./', cliUrl);
const indexUrl = new URL('./index.js', distDirUrl).href;
const typesUrl = new URL('./types/index.js', distDirUrl).href;

// Load zod from *atproto-mcp's* dependency tree (not from this repo).
const requireFromAtproto = createRequire(cliUrl);
const { z } = requireFromAtproto('zod');

// Import dependencies from the atproto-mcp package so our patch uses the same
// McpError class as the server.
const [{ McpError }, { AtpMcpServer }, { runCli }] = await Promise.all([
  import(typesUrl),
  import(indexUrl),
  import(cliUrl),
]);

// Work around a bug in atproto-mcp where it registers one `tools/call` request
// handler per tool. The MCP SDK routes by `method`, so the last registration
// wins and only one tool becomes callable (often `extract_media_from_post`),
// producing errors like:
//   Invalid literal value, expected "extract_media_from_post"
//
// Patch by registering a single handler and dispatching by `params.name`.
const _originalRegisterTools = AtpMcpServer.prototype.registerTools;
const _originalSrc = String(_originalRegisterTools || '');
const _looksBroken =
  _originalSrc.includes("method: z.literal('tools/call')") &&
  _originalSrc.includes('name: z.literal(tool.schema.method)');

if (_looksBroken) {
  AtpMcpServer.prototype.registerTools = function registerTools(tools) {
  this.server.setRequestHandler(z.object({ method: z.literal('tools/list') }), async () => ({
    tools: tools.map((tool) => ({
      name: tool.schema.method,
      description: tool.schema.description || '',
      inputSchema: tool.schema.params ? this.zodToJsonSchema(tool.schema.params) : undefined,
    })),
  }));

  const toolByName = new Map(tools.map((tool) => [tool.schema.method, tool]));
  this.server.setRequestHandler(
    z.object({
      method: z.literal('tools/call'),
      params: z.object({
        name: z.string(),
        arguments: z.any().optional(),
      }),
    }),
    async (request) => {
      const toolName = request.params.name;
      const tool = toolByName.get(toolName);
      if (!tool) {
        throw new McpError(`Unknown tool: ${toolName}`, -32601, { tool: toolName });
      }

      try {
        if ('isAvailable' in tool && typeof tool.isAvailable === 'function') {
          if (!tool.isAvailable()) {
            const availabilityMessage =
              'getAvailabilityMessage' in tool && typeof tool.getAvailabilityMessage === 'function'
                ? tool.getAvailabilityMessage()
                : 'Tool not available';
            throw new McpError(`Tool not available: ${availabilityMessage}`, -32603, {
              tool: tool.schema.method,
              availability: availabilityMessage,
            });
          }
        }

        const result = await tool.handler(request.params.arguments || {});
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(result, null, 2),
            },
          ],
        };
      } catch (error) {
        this.logger.error(`Tool ${tool.schema.method} execution failed`, error);
        throw new McpError(
          `Tool execution failed: ${error instanceof Error ? error.message : 'Unknown error'}`,
          -32603,
          {
            tool: tool.schema.method,
            error: error instanceof Error ? error.message : String(error),
          }
        );
      }
    }
  );

  this.logger.info(`Registered ${tools.length} MCP tools`);
  };
}

// Ensure the atproto-mcp CLI sees the expected argv shape.
// `parseArgs()` defaults to `process.argv.slice(2)`, so we keep user args intact.
process.argv[1] = path.resolve(cliPath);

await runCli();
