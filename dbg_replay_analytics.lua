local widget = widget ---@type Widget

function widget:GetInfo()
	return {
		name = "Replay Analytics",
		desc = "Logs unit, economy and builder events of a replay to <replay name>_events.json",
		author = "Backbone",
		date = "October 2026",
		license = "GNU GPL, v2 or later",
		layer = -100000,
		enabled = true,
	}
end

-- Logs the events of a replay played back in spring-headless. Unsynced, so it cannot desync.
-- Active only in a headless replay; in a normal client, also when watching a replay, it removes itself.
-- Writes events/<replay name>_events.json in the widget's own folder (BAR lets user widgets write only under
-- LuaUI/Config/ and LuaUI/Widgets/), one JSON object:
--   {"events":[...],"game":{...},"teams":[...],"teamStats":{"<teamID>":[...]},"unitDefs":{"<unitDefID>":{...}},
--    "end":{"frame":...,"reason":...}}
-- Each event is {"frame":...,"<event name>":{...}}, one per line, named and with fields named after the call-in or
-- Spring function it comes from. All events come from call-ins, nothing is sampled. What does not change
-- during the game is written once, after the events: the game, the teams, each team's statistics history
-- (Spring.GetTeamStatsHistory, every 15 s) and the unit defs the events refer to. "end" is written when logging stops,
-- also when the replay stalls;
-- its reason is "gameover" only if the game ended. A file without it is incomplete: the engine crashed or was stopped.
-- Switches to full view, raises the speed limit (the pre-game start position phase of a replay otherwise plays in real
-- time), skips to the end of the replay once the game runs, and quits the engine when the game is over or the replay
-- stops advancing.

--------------------------------------------------------------------------------
-- Configuration
--------------------------------------------------------------------------------

local STALL_QUIT_SECONDS = 20 -- the replay has ended when frames stop advancing
local PREGAME_QUIT_SECONDS = 300

--------------------------------------------------------------------------------
-- Engine API and state
--------------------------------------------------------------------------------

local spDiffTimers = Spring.DiffTimers
local spEcho = Spring.Echo
local spGetAIInfo = Spring.GetAIInfo
local spGetFactoryCommandCount = Spring.GetFactoryCommandCount
local spGetGaiaTeamID = Spring.GetGaiaTeamID
local spGetGameFrame = Spring.GetGameFrame
local spGetPlayerInfo = Spring.GetPlayerInfo
local spGetTeamInfo = Spring.GetTeamInfo
local spGetTeamList = Spring.GetTeamList
local spGetTeamRulesParam = Spring.GetTeamRulesParam
local spGetTeamStatsHistory = Spring.GetTeamStatsHistory
local spGetTimer = Spring.GetTimer
local spGetUnitCommandCount = Spring.GetUnitCommandCount
local spGetUnitHealth = Spring.GetUnitHealth
local spGetUnitIsBeingBuilt = Spring.GetUnitIsBeingBuilt
local spGetUnitPosition = Spring.GetUnitPosition
local spIsReplay = Spring.IsReplay
local spSendCommands = Spring.SendCommands

local mathAbs = math.abs
local mathFloor = math.floor
local mathHuge = math.huge
local stringFormat = string.format

-- The replay's file name without .sdfz (Spring.GetReplayFilePath() returns nil once the replay has ended).
local EVENTS_FILE_NAME = (Game.demoPlayName or "replay") .. "_events.json"

-- Unit def lookups, filled in Initialize.
local isFactoryDef = {}
local isBuilderDef = {} -- mobile builders: commanders and constructors; nano turrets are not followed

local eventsFile -- nil while not logging
local isFirstEvent = true
local teamIDs = {} -- set of every team but Gaia
local referencedUnitDefIDs = {} -- set of the unit defs the events refer to, written once at the end

local lastFrame = -1
local lastFrameTimer -- when the frame last advanced; nil until Initialize
local isSpeedRaised = false
local isSkipSent = false
local isQuitting = false

--------------------------------------------------------------------------------
-- Event output
--------------------------------------------------------------------------------

local objectMetatable = {}

-- Marks a table to be written as a JSON object also when it is empty; an empty table is written as [] otherwise.
local function jsonObject(value)
	return setmetatable(value, objectMetatable)
end

-- Own encoder: BAR's Json.encode writes NaN and infinity as invalid JSON and floats with 14 digits.
local function encodeJson(value)
	local valueType = type(value)
	if valueType == "number" then
		if value ~= value or value == mathHuge or value == -mathHuge then
			return "null"
		end
		if value == mathFloor(value) and mathAbs(value) < 1e15 then
			return stringFormat("%d", value)
		end
		return stringFormat("%.3f", value)
	elseif valueType == "string" then
		local escaped = value:gsub('[%c"\\]', function(char)
			return stringFormat("\\u%04x", char:byte())
		end)
		return '"' .. escaped .. '"'
	elseif valueType == "boolean" then
		return tostring(value)
	elseif valueType == "table" then
		local parts = {}
		if getmetatable(value) ~= objectMetatable and (#value > 0 or next(value) == nil) then
			for i = 1, #value do
				parts[i] = encodeJson(value[i])
			end
			return "[" .. table.concat(parts, ",") .. "]"
		end
		for key, element in pairs(value) do
			parts[#parts + 1] = encodeJson(tostring(key)) .. ":" .. encodeJson(element)
		end
		return "{" .. table.concat(parts, ",") .. "}"
	end
	return "null"
end

-- Writes {"frame":...,"<name>":<fields>} into the events array.
local function writeEvent(name, fields)
	if not eventsFile then
		return
	end
	local separator = isFirstEvent and "" or ",\n"
	isFirstEvent = false
	eventsFile:write(separator, stringFormat('{"frame":%d,%s:%s}', spGetGameFrame(), encodeJson(name), encodeJson(fields)))
end

-- Notes that the events refer to a unit def, so it is written at the end. Returns the unitDefID.
local function referenceUnitDef(unitDefID)
	if unitDefID and UnitDefs[unitDefID] then
		referencedUnitDefIDs[unitDefID] = true
	end
	return unitDefID
end

--------------------------------------------------------------------------------
-- Once per game: game, teams and unit defs
--------------------------------------------------------------------------------

local function getGame()
	return {
		mapName = Game.mapName,
		mapSizeX = Game.mapSizeX,
		mapSizeZ = Game.mapSizeZ,
		gameName = Game.gameName,
		gameVersion = Game.gameVersion,
		maxUnits = Game.maxUnits,
	}
end

local function getTeamName(teamID, leaderID, isAI)
	if isAI then
		local _, aiName, _, shortName = spGetAIInfo(teamID)
		return (aiName or "AI") .. " (" .. tostring(shortName) .. ")"
	end
	return spGetPlayerInfo(leaderID, false)
end

-- Every team but Gaia, with the commander it started with: BAR's team rules param "startUnit" (a unitDefID), which
-- also tells the faction of a team whose side is "random".
local function getTeams()
	local teams = {}
	for _, teamID in ipairs(spGetTeamList()) do
		if teamIDs[teamID] then
			local _, leaderID, _, isAI, side, allyTeamID = spGetTeamInfo(teamID, false)
			teams[#teams + 1] = {
				teamID = teamID,
				allyTeam = allyTeamID,
				isAI = isAI,
				side = side,
				startUnit = referenceUnitDef(tonumber(spGetTeamRulesParam(teamID, "startUnit"))),
				name = getTeamName(teamID, leaderID, isAI),
			}
		end
	end
	return teams
end

-- The unit defs the events refer to, keyed by unitDefID, with the fields of UnitDefs the analysis needs.
local function getUnitDefs()
	local unitDefs = jsonObject({})
	for unitDefID in pairs(referencedUnitDefIDs) do
		local unitDef = UnitDefs[unitDefID]
		local customParams = unitDef.customParams
		unitDefs[tostring(unitDefID)] = {
			name = unitDef.name,
			translatedHumanName = unitDef.translatedHumanName or unitDef.humanName,
			metalCost = unitDef.metalCost,
			energyCost = unitDef.energyCost,
			buildTime = unitDef.buildTime,
			isFactory = unitDef.isFactory,
			isBuilder = unitDef.isBuilder,
			canMove = unitDef.canMove,
			buildDistance = unitDef.buildDistance,
			canResurrect = unitDef.canResurrect,
			extractsMetal = unitDef.extractsMetal,
			energyMake = unitDef.energyMake,
			windGenerator = unitDef.windGenerator,
			maxWeaponRange = unitDef.maxWeaponRange,
			customParams = jsonObject({
				techlevel = customParams.techlevel,
				unitgroup = customParams.unitgroup,
				iscommander = customParams.iscommander,
			}),
		}
	end
	return unitDefs
end

-- Every team's statistics history by teamID, as Spring.GetTeamStatsHistory returns it: an entry every 15 s with the
-- totals since the start of the game, the last one at the current frame.
local function getTeamStats()
	local teamStats = jsonObject({})
	for teamID in pairs(teamIDs) do
		local count = spGetTeamStatsHistory(teamID) or 0
		teamStats[tostring(teamID)] = count > 0 and spGetTeamStatsHistory(teamID, 1, count) or {}
	end
	return teamStats
end

-- Closes the events array and writes what is written once. The teams come first: they refer to unit defs.
local function closeEventsFile(reason)
	if not eventsFile then
		return
	end
	local teams = getTeams()
	eventsFile:write(
		'\n],"game":',
		encodeJson(getGame()),
		',"teams":',
		encodeJson(teams),
		',"teamStats":',
		encodeJson(getTeamStats()),
		',"unitDefs":',
		encodeJson(getUnitDefs()),
		',"end":',
		encodeJson({ frame = spGetGameFrame(), reason = reason }),
		"}\n"
	)
	eventsFile:close()
	eventsFile = nil
end

--------------------------------------------------------------------------------
-- Units and teams
--------------------------------------------------------------------------------

-- Adds the unit's map position in elmos, rounded down, to fields as x and z.
local function addUnitPosition(fields, unitID)
	local x, _, z = spGetUnitPosition(unitID)
	if x then
		fields.x = mathFloor(x)
		fields.z = mathFloor(z)
	end
	return fields
end

local function classifyUnitDefs()
	for unitDefID, unitDef in pairs(UnitDefs) do
		if unitDef.isFactory then
			isFactoryDef[unitDefID] = true
		elseif unitDef.isBuilder and unitDef.canMove then
			isBuilderDef[unitDefID] = true
		end
	end
end

local function collectTeamIDs()
	local gaiaTeamID = spGetGaiaTeamID()
	for _, teamID in ipairs(spGetTeamList()) do
		if teamID ~= gaiaTeamID then
			teamIDs[teamID] = true
		end
	end
end

--------------------------------------------------------------------------------
-- Command queues
--------------------------------------------------------------------------------

-- Returns the number of commands in the unit's queue. A factory's queue is its build queue: Spring.GetUnitCommandCount
-- gives the orders for the units it builds.
local function getQueueCount(unitID, isFactory)
	if isFactory then
		return spGetFactoryCommandCount(unitID) or 0
	end
	return spGetUnitCommandCount(unitID) or 0
end

-- Logs "unitIdle" for a finished commander, constructor or factory without orders. The engine calls UnitIdle only
-- when a unit's queue runs empty, not for a unit that is finished or given to a team without orders.
local function logIdleIfNoOrders(unitID, unitDefID, unitTeam)
	local isFactory = isFactoryDef[unitDefID]
	if (isFactory or isBuilderDef[unitDefID]) and getQueueCount(unitID, isFactory) == 0 then
		writeEvent("unitIdle", { unitID = unitID, unitDefID = unitDefID, unitTeam = unitTeam })
	end
end

local function quit(reason)
	if isQuitting then
		return
	end
	isQuitting = true
	closeEventsFile(reason)
	spEcho("Replay Analytics: done (" .. reason .. ")")
	spSendCommands("quitforce")
end

--------------------------------------------------------------------------------
-- Call-ins
--------------------------------------------------------------------------------

function widget:Initialize()
	if not (spIsReplay() and Platform.isHeadless) then
		widgetHandler:RemoveWidget()
		return
	end
	local eventsFolder = (widget.whInfo.filename:match("^(.*[/\\])") or "") .. "events/"
	Spring.CreateDir(eventsFolder)
	local eventsPath = eventsFolder .. EVENTS_FILE_NAME
	eventsFile = io.open(eventsPath, "w")
	if not eventsFile then
		spEcho("Replay Analytics: cannot write " .. eventsPath)
		widgetHandler:RemoveWidget()
		return
	end
	eventsFile:write('{"events":[\n')
	classifyUnitDefs()
	collectTeamIDs()
	spSendCommands("specfullview 1")
	lastFrameTimer = spGetTimer()
end

function widget:Shutdown()
	closeEventsFile("shutdown")
end

function widget:Update()
	if isQuitting or not lastFrameTimer then
		return
	end
	-- A server command the local player may send; the speed limit also applies before the game starts.
	if not isSpeedRaised then
		isSpeedRaised = true
		spSendCommands("setmaxspeed 1000", "setspeed 1000")
	end
	local frame = spGetGameFrame()
	if frame ~= lastFrame then
		lastFrame = frame
		lastFrameTimer = spGetTimer()
		return
	end
	local quitSeconds = frame > 0 and STALL_QUIT_SECONDS or PREGAME_QUIT_SECONDS
	if spDiffTimers(spGetTimer(), lastFrameTimer) > quitSeconds then
		quit(frame > 0 and "stalled" or "never started")
	end
end

function widget:GameFrame(frame)
	-- The skip only works once the game runs; the server then sends the rest of the replay as fast as it can.
	if not isSkipSent and frame >= 1 then
		isSkipSent = true
		spSendCommands("skip f99999999")
	end
end

function widget:UnitCreated(unitID, unitDefID, unitTeam, builderID)
	writeEvent(
		"unitCreated",
		addUnitPosition({
			unitID = unitID,
			unitDefID = referenceUnitDef(unitDefID),
			unitTeam = unitTeam,
			builderID = builderID,
		}, unitID)
	)
end

function widget:UnitFinished(unitID, unitDefID, unitTeam)
	writeEvent("unitFinished", { unitID = unitID, unitDefID = referenceUnitDef(unitDefID), unitTeam = unitTeam })
	logIdleIfNoOrders(unitID, unitDefID, unitTeam)
end

function widget:UnitDestroyed(unitID, unitDefID, unitTeam, attackerID, attackerDefID, attackerTeam, weaponDefID)
	local _, _, _, _, buildProgress = spGetUnitHealth(unitID)
	writeEvent(
		"unitDestroyed",
		addUnitPosition({
			unitID = unitID,
			unitDefID = referenceUnitDef(unitDefID),
			unitTeam = unitTeam,
			buildProgress = buildProgress,
			attackerID = attackerID,
			attackerDefID = referenceUnitDef(attackerDefID),
			attackerTeam = attackerTeam,
			weaponDefID = weaponDefID,
		}, unitID)
	)
end

function widget:UnitGiven(unitID, unitDefID, newTeam, oldTeam)
	writeEvent(
		"unitGiven",
		{ unitID = unitID, unitDefID = referenceUnitDef(unitDefID), newTeam = newTeam, oldTeam = oldTeam }
	)
	if not spGetUnitIsBeingBuilt(unitID) then
		logIdleIfNoOrders(unitID, unitDefID, newTeam)
	end
end

-- The command call-ins are logged for commanders, constructors and factories only: they tell when a builder is idle
-- and what it was ordered to do. cmdID < 0 is a build of unit def -cmdID.
local function isCommandLogged(unitDefID)
	return isBuilderDef[unitDefID] or isFactoryDef[unitDefID]
end

function widget:UnitIdle(unitID, unitDefID, unitTeam)
	if isCommandLogged(unitDefID) then
		writeEvent("unitIdle", { unitID = unitID, unitDefID = unitDefID, unitTeam = unitTeam })
	end
end

function widget:UnitCommand(unitID, unitDefID, unitTeam, cmdID, cmdParams, cmdOpts, _, playerID, fromSynced, fromLua)
	if isCommandLogged(unitDefID) then
		writeEvent("unitCommand", {
			unitID = unitID,
			unitDefID = unitDefID,
			unitTeam = unitTeam,
			cmdID = cmdID < 0 and -referenceUnitDef(-cmdID) or cmdID,
			cmdParams = cmdParams,
			cmdOpts = cmdOpts and cmdOpts.coded,
			playerID = playerID,
			fromSynced = fromSynced,
			fromLua = fromLua,
		})
	end
end

function widget:UnitCmdDone(unitID, unitDefID, unitTeam, cmdID, cmdParams)
	if isCommandLogged(unitDefID) then
		writeEvent("unitCmdDone", {
			unitID = unitID,
			unitDefID = unitDefID,
			unitTeam = unitTeam,
			cmdID = cmdID < 0 and -referenceUnitDef(-cmdID) or cmdID,
			cmdParams = cmdParams,
		})
	end
end

function widget:TeamDied(teamID)
	writeEvent("teamDied", { teamID = teamID })
end

function widget:GameOver(winningAllyTeams)
	writeEvent("gameOver", { winningAllyTeams = winningAllyTeams })
	quit("gameover")
end
