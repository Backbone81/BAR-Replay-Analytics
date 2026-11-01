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
--   {"events":[...],"game":{...},"teams":[...],"unitDefs":{"<unitDefID>":{...}},"end":{"frame":...,"reason":...}}
-- Each event is {"frame":...,"<event name>":{...}}, one per line, named and with fields named after the call-in or
-- Spring function it comes from. What does not change during the game is written once, after the events: the game,
-- the teams, and the unit defs the events refer to. "end" is written when logging stops, also when the replay stalls;
-- its reason is "gameover" only if the game ended. A file without it is incomplete: the engine crashed or was stopped.
-- Switches to full view, raises the speed limit (the pre-game start position phase of a replay otherwise plays in real
-- time), skips to the end of the replay once the game runs, and quits the engine when the game is over or the replay
-- stops advancing.

--------------------------------------------------------------------------------
-- Configuration
--------------------------------------------------------------------------------

local RESOURCES_SECONDS = 5 -- "teamResources" events
local BUILDER_SAMPLE_SECONDS = 0.5 -- "builderState" events are written on change only
local STALL_QUIT_SECONDS = 20 -- the replay has ended when frames stop advancing
local PREGAME_QUIT_SECONDS = 300

--------------------------------------------------------------------------------
-- Engine API and state
--------------------------------------------------------------------------------

local spDiffTimers = Spring.DiffTimers
local spEcho = Spring.Echo
local spGetAIInfo = Spring.GetAIInfo
local spGetFactoryCommandCount = Spring.GetFactoryCommandCount
local spGetFactoryCommands = Spring.GetFactoryCommands
local spGetGaiaTeamID = Spring.GetGaiaTeamID
local spGetGameFrame = Spring.GetGameFrame
local spGetPlayerInfo = Spring.GetPlayerInfo
local spGetTeamInfo = Spring.GetTeamInfo
local spGetTeamList = Spring.GetTeamList
local spGetTeamResources = Spring.GetTeamResources
local spGetTeamRulesParam = Spring.GetTeamRulesParam
local spGetTimer = Spring.GetTimer
local spGetUnitCommandCount = Spring.GetUnitCommandCount
local spGetUnitCurrentCommand = Spring.GetUnitCurrentCommand
local spGetUnitHealth = Spring.GetUnitHealth
local spGetUnitIsBeingBuilt = Spring.GetUnitIsBeingBuilt
local spGetUnitPosition = Spring.GetUnitPosition
local spGetUnitWorkerTask = Spring.GetUnitWorkerTask
local spIsReplay = Spring.IsReplay
local spSendCommands = Spring.SendCommands

local mathAbs = math.abs
local mathFloor = math.floor
local mathHuge = math.huge
local stringFormat = string.format

local GAME_SPEED = Game.gameSpeed
local RESOURCES_FRAMES = RESOURCES_SECONDS * GAME_SPEED
local BUILDER_SAMPLE_FRAMES = mathFloor(BUILDER_SAMPLE_SECONDS * GAME_SPEED)

-- The replay's file name without .sdfz (Spring.GetReplayFilePath() returns nil once the replay has ended).
local EVENTS_FILE_NAME = (Game.demoPlayName or "replay") .. "_events.json"

-- Unit def lookups, filled in Initialize.
local isFactoryDef = {}
local isBuilderDef = {} -- mobile builders: commanders and constructors
local isNanoDef = {} -- builders that can't move

local eventsFile -- nil while not logging
local isFirstEvent = true
local teamIDs = {} -- set of every team but Gaia
local referencedUnitDefIDs = {} -- set of the unit defs the events refer to, written once at the end

-- unitID -> teamID
local factories = {}
local builders = {}
local nanos = {}

-- unitID -> last logged "builderState" fields
local builderStates = {}
local builderCommands = {}
local builderTargets = {}

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
		elseif unitDef.isBuilder then
			isNanoDef[unitDefID] = true
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

local function addUnit(unitID, unitDefID, teamID)
	if isFactoryDef[unitDefID] then
		factories[unitID] = teamID
	elseif isBuilderDef[unitDefID] then
		builders[unitID] = teamID
	elseif isNanoDef[unitDefID] then
		nanos[unitID] = teamID
	end
end

local function removeUnit(unitID)
	factories[unitID] = nil
	builders[unitID] = nil
	nanos[unitID] = nil
	builderStates[unitID] = nil
	builderCommands[unitID] = nil
	builderTargets[unitID] = nil
end

--------------------------------------------------------------------------------
-- Sampling
--------------------------------------------------------------------------------

-- Returns the number of commands in the unit's queue and the cmdID of the first. A factory's queue is its build queue:
-- Spring.GetUnitCommandCount gives the orders for the units it builds.
local function getQueue(unitID, isFactory)
	if isFactory then
		local count = spGetFactoryCommandCount(unitID) or 0
		local commands = count > 0 and spGetFactoryCommands(unitID, 1)
		return count, commands and commands[1] and commands[1].id
	end
	local count = spGetUnitCommandCount(unitID) or 0
	return count, count > 0 and spGetUnitCurrentCommand(unitID) or nil
end

-- Logs a "builderState" event when a finished builder's or factory's state changes: "working" (cmdID and targetID of
-- Spring.GetUnitWorkerTask: build = -unitDefID, 40 repair or assist, 90 reclaim, 125 resurrect; a feature's targetID
-- is featureID + Game.maxUnits), "busy" (orders but no worker task, e.g. walking; cmdID: the first one) or "idle" (no
-- orders).
local function logBuilderState(unitID, isFactory)
	if spGetUnitIsBeingBuilt(unitID) then
		return
	end
	local state, cmdID, targetID
	local taskCmdID, taskTargetID = spGetUnitWorkerTask(unitID)
	if taskCmdID then
		state, cmdID, targetID = "working", taskCmdID, taskTargetID
	else
		local count, firstCmdID = getQueue(unitID, isFactory)
		if count > 0 then
			state, cmdID = "busy", firstCmdID
		else
			state = "idle"
		end
	end
	if builderStates[unitID] == state and builderCommands[unitID] == cmdID and builderTargets[unitID] == targetID then
		return
	end
	builderStates[unitID] = state
	builderCommands[unitID] = cmdID
	builderTargets[unitID] = targetID
	if cmdID and cmdID < 0 then
		referenceUnitDef(-cmdID)
	end
	writeEvent("builderState", { unitID = unitID, state = state, cmdID = cmdID, targetID = targetID })
end

-- The first five values of Spring.GetTeamResources.
local function getTeamResource(teamID, resource)
	local currentLevel, storage, pull, income, expense = spGetTeamResources(teamID, resource)
	return { currentLevel = currentLevel, storage = storage, pull = pull, income = income, expense = expense }
end

local function logTeamResources()
	for teamID in pairs(teamIDs) do
		writeEvent("teamResources", {
			teamID = teamID,
			metal = getTeamResource(teamID, "metal"),
			energy = getTeamResource(teamID, "energy"),
		})
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
	if frame % BUILDER_SAMPLE_FRAMES == 0 then
		for unitID in pairs(builders) do
			logBuilderState(unitID, false)
		end
		for unitID in pairs(nanos) do
			logBuilderState(unitID, false)
		end
		for unitID in pairs(factories) do
			logBuilderState(unitID, true)
		end
	end
	if frame % RESOURCES_FRAMES == 0 then
		logTeamResources()
	end
end

function widget:UnitCreated(unitID, unitDefID, unitTeam, builderID)
	addUnit(unitID, unitDefID, unitTeam)
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
	removeUnit(unitID)
end

function widget:UnitGiven(unitID, unitDefID, newTeam, oldTeam)
	removeUnit(unitID)
	addUnit(unitID, unitDefID, newTeam)
	writeEvent(
		"unitGiven",
		{ unitID = unitID, unitDefID = referenceUnitDef(unitDefID), newTeam = newTeam, oldTeam = oldTeam }
	)
end

function widget:GameOver(winningAllyTeams)
	writeEvent("gameOver", { winningAllyTeams = winningAllyTeams })
	quit("gameover")
end
