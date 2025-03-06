import os
import discord
from discord.ext import commands
import random
from enum import Enum
from typing import Dict, List, Optional, Union
import asyncio
from dotenv import load_dotenv
from agent import MistralAgent
from discord.ui import Select, View

# Load environment variables
load_dotenv()

# Initialize Mistral client
MISTRAL_MODEL = "mistral-large-latest"

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Game constants
MIN_PLAYERS = 4  # Minimum number of players required to start a game (1 Mafia, 1 Detective, 1 Doctor, 1 Villager)

class Role(Enum):
    VILLAGER = 1
    MAFIA = 2
    DETECTIVE = 3
    DOCTOR = 4

class GameState(Enum):
    WAITING = 1
    IN_PROGRESS = 2
    DAY = 3
    NIGHT = 4
    ENDED = 5
    VOTING = 6

class StoryTeller:
    def __init__(self):
        self.agent = MistralAgent()
        
    async def generate_story(self, prompt: str, max_length: int = 1900) -> str:
        """Generate a story using Mistral AI"""
        try:
            # Create a mock discord message for the agent
            mock_message = type('MockMessage', (), {'content': prompt})()
            response = await self.agent.run(mock_message)
            
            if not response:
                return "The village continues its story..."  # Fallback if empty response
            
            # If response is too long, ask Mistral to shorten it
            if len(response) > max_length:  # Leave buffer for safety
                shorten_prompt = f"Please shorten this story while keeping the main dramatic elements (keep it under {max_length} characters):\n\n{response}"
                mock_message.content = shorten_prompt
                response = await self.agent.run(mock_message)
                
                if not response or len(response) > max_length:
                    # If still too long or empty, use first max_length chars as fallback
                    return response[:max_length]
                    
            return response
                
        except Exception as e:
            print(f"Error generating story: {e}")
            return "The village continues its story..."  # Fallback message if anything goes wrong

class NPCPlayer:
    def __init__(self, name: str, player_id: int):
        self.name = name
        self.id = player_id
        
    async def send(self, message: str):
        # NPCs don't need to receive messages
        pass

class PollSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view  # Store reference to parent view
        super().__init__(
            placeholder="Make your selection...",
            min_values=1,
            max_values=1,
            options=[],
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle vote selection"""
        try:
            # Store the vote
            self.parent_view.votes[interaction.user.id] = int(self.values[0])
            
            # Acknowledge the vote
            await interaction.response.send_message(f"Your vote has been recorded!", ephemeral=True)
            
        except Exception as e:
            print(f"Error in poll callback: {e}")
            await interaction.response.send_message("There was an error recording your vote.", ephemeral=True)

class PollView(discord.ui.View):
    def __init__(self, timeout=30):
        super().__init__(timeout=timeout)
        self.votes = {}
        self.select = PollSelect(self)  # Create select with reference to this view
        self.add_item(self.select)  # Add select to view

    def get_votes(self):
        """Return the current votes"""
        return self.votes

    async def on_timeout(self):
        """Handle timeout of the poll"""
        for item in self.children:
            item.disabled = True

class KillView(View):
    def __init__(self, game, alive_players):
        super().__init__(timeout=45)
        self.game = game
        
        # Create the select menu
        select = Select(
            placeholder="Choose a player to kill...",
            options=[
                discord.SelectOption(
                    label=game.players[pid].display_name,
                    value=str(pid)
                ) for pid in alive_players
            ]
        )
        
        async def kill_callback(interaction):
            if interaction.user.id not in [pid for pid, role in game.player_roles.items() if role == Role.MAFIA]:
                await interaction.response.send_message("You are not authorized to make this selection!", ephemeral=True)
                return
                
            target_id = int(select.values[0])
            game.night_actions[interaction.user.id] = {
                'action': 'kill',
                'target': target_id
            }
            await interaction.response.send_message(f"You have chosen to kill {game.players[target_id].display_name}", ephemeral=True)
            self.stop()
            
        select.callback = kill_callback
        self.add_item(select)

class ProtectView(View):
    def __init__(self, game, alive_players):
        super().__init__(timeout=45)
        self.game = game
        
        select = Select(
            placeholder="Choose a player to protect...",
            options=[
                discord.SelectOption(
                    label=game.players[pid].display_name,
                    value=str(pid)
                ) for pid in alive_players
            ]
        )
        
        async def protect_callback(interaction):
            if interaction.user.id not in [pid for pid, role in game.player_roles.items() if role == Role.DOCTOR]:
                await interaction.response.send_message("You are not authorized to make this selection!", ephemeral=True)
                return
                
            target_id = int(select.values[0])
            game.night_actions[interaction.user.id] = {
                'action': 'protect',
                'target': target_id
            }
            await interaction.response.send_message(f"You have chosen to protect {game.players[target_id].display_name}", ephemeral=True)
            self.stop()
            
        select.callback = protect_callback
        self.add_item(select)

class InvestigateView(View):
    def __init__(self, game, alive_players):
        super().__init__(timeout=45)
        self.game = game
        
        select = Select(
            placeholder="Choose a player to investigate...",
            options=[
                discord.SelectOption(
                    label=game.players[pid].display_name,
                    value=str(pid)
                ) for pid in alive_players
            ]
        )
        
        async def investigate_callback(interaction):
            if interaction.user.id not in [pid for pid, role in game.player_roles.items() if role == Role.DETECTIVE]:
                await interaction.response.send_message("You are not authorized to make this selection!", ephemeral=True)
                return
                
            target_id = int(select.values[0])
            game.night_actions[interaction.user.id] = {
                'action': 'investigate',
                'target': target_id
            }
            await interaction.response.send_message(f"You have chosen to investigate {game.players[target_id].display_name}", ephemeral=True)
            self.stop()
            
        select.callback = investigate_callback
        self.add_item(select)

class MafiaGame:
    def __init__(self, guild, channel):
        self.guild = guild
        self.main_channel = channel
        self.players = {}
        self.player_roles = {}
        self.alive_players = []
        self.dead_players = []
        self.mafia_channel = None
        self.detective_channel = None
        self.doctor_channel = None
        self.night_actions = {}
        self.is_night = False
        self.active_polls = {}
        self.game_started = False
        self.state = GameState.WAITING
        self.current_votes = {}

    async def begin_game(self):
        """Start the game and assign roles to players"""
        if len(self.players) < MIN_PLAYERS:
            return await self.main_channel.send(f"Not enough players to start the game. Minimum required: {MIN_PLAYERS}")
        
        # Assign roles first
        self.assign_roles()
        
        # Create role channels after roles are assigned
        await self.create_role_channels()
        
        # Send role information to players
        for player_id, role in self.player_roles.items():
            player = self.players[player_id]
            await player.send(f"Your role is: {role.name}")
        
        self.game_started = True
        self.state = GameState.IN_PROGRESS
        self.alive_players = list(self.players.keys())
        
        # Announce game start
        await self.main_channel.send("The game has begun! Check your DMs for your role.")
        await self.start_night()

    def generate_npc_name(self) -> str:
        """Generate a random medieval-style name for an NPC"""
        first_names = ["Aldrich", "Bartholomew", "Constantine", "Darius", "Edmund", "Felix", "Galahad", "Henrik"]
        surnames = ["Blackwood", "Crowley", "Darkshire", "Elderworth", "Frostweaver", "Grimsworth", "Hawthorne"]
        return f"{random.choice(first_names)} {random.choice(surnames)}"

    async def add_npcs_if_needed(self):
        """Add NPC players if there aren't enough real players"""
        min_players = 4  # Minimum required for a balanced game
        real_players = len([p for p in self.players.keys() if p < self.npc_base_id])
        print(f"Adding NPCs. Current real players: {real_players}")
        
        while len(self.players) < min_players:
            npc_id = self.npc_base_id + self.npc_count
            npc_name = self.generate_npc_name()
            npc = NPCPlayer(npc_name, npc_id)
            self.players[npc_id] = npc
            self.npc_count += 1
            print(f"Added NPC: {npc_name} (ID: {npc_id})")
            
            if self.main_channel:
                try:
                    if self.storyteller:
                        # Generate introduction story for NPC
                        prompt = f"Create a brief one-sentence introduction for {npc_name}, a stranger who has joined the village."
                        story = await self.storyteller.generate_story(prompt)
                        await self.main_channel.send(story)
                    else:
                        await self.main_channel.send(f"{npc_name} has joined the village.")
                except Exception as e:
                    print(f"Error introducing NPC: {e}")
                    await self.main_channel.send(f"{npc_name} has joined the village.")
                    
    def assign_roles(self):
        """Assign roles to all players randomly"""
        print("Assigning roles to players...")
        
        # Get list of all players
        player_ids = list(self.players.keys())
        if len(player_ids) < 4:
            raise ValueError("Not enough players to assign roles")
            
        # Calculate number of each role based on player count
        num_players = len(player_ids)
        num_mafia = max(1, num_players // 4)  # At least 1 mafia
        
        # Create list of all roles
        roles = []
        roles.extend([Role.MAFIA] * num_mafia)  # Add mafia roles
        roles.append(Role.DETECTIVE)  # Add one detective
        roles.append(Role.DOCTOR)  # Add one doctor
        
        # Fill remaining slots with villagers
        num_villagers = num_players - len(roles)
        roles.extend([Role.VILLAGER] * num_villagers)
        
        # Shuffle both the player IDs and roles
        random.shuffle(player_ids)
        random.shuffle(roles)
        
        # Reset roles and assign new ones
        self.player_roles.clear()
        for player_id, role in zip(player_ids, roles):
            self.player_roles[player_id] = role
            player_name = self.players[player_id].name
            print(f"Assigned {role.value} to {player_name}")
        
        print("Player Roles:")
        for player_id, role in self.player_roles.items():
            player_name = self.players[player_id].name
            print(f"{player_name}: {role.value}")
        
        # Set initial alive players
        self.alive_players = player_ids.copy()
        self.dead_players = []

    async def send_role_dms(self):
        """Send role information to all players"""
        print("Sending role DMs...")
        
        # First, collect all mafia members for the mafia message
        mafia_members = [self.players[pid].name for pid, role in self.player_roles.items() 
                        if role == Role.MAFIA and pid < self.npc_base_id]
        
        for player_id, role in self.player_roles.items():
            if player_id >= self.npc_base_id:  # Skip NPCs
                continue
                
            try:
                player = self.players[player_id]
                role_msg = f"Your role is: {role.value}"
                
                # Add mafia member list for mafia players
                if role == Role.MAFIA and len(mafia_members) > 1:
                    other_mafia = [name for name in mafia_members if name != player.name]
                    if other_mafia:
                        role_msg += f"\nOther mafia members: {', '.join(other_mafia)}"
                
                await player.send(role_msg)
                print(f"Sent role DM to {player_id}")
            except Exception as e:
                print(f"Error sending role DM to {player_id}: {e}")
                
    async def handle_action_vote(self, player_id: int, action_type: str, target_id: int):
        """Handle a vote for a night action"""
        try:
            if action_type in ['kill', 'investigate', 'save']:
                # Create a new entry for this player if it doesn't exist
                if player_id not in self.night_actions:
                    self.night_actions[player_id] = {}
                
                # Store the action and target
                self.night_actions[player_id] = {
                    'action': action_type,
                    'target': target_id
                }
                
                # Debug prints
                voter = self.players[player_id].name
                target = self.players[target_id].name
                print(f"DEBUG - Vote registered: {voter} ({self.player_roles[player_id].value}) voted to {action_type} {target}")
                return True
            return False
        except Exception as e:
            print(f"ERROR in handle_action_vote: {e}")
            print(f"player_id: {player_id}, action_type: {action_type}, target_id: {target_id}")
            return False

    async def process_votes(self, poll_id):
        """Process votes from a poll"""
        try:
            if poll_id not in self.active_polls:
                print(f"ERROR: Poll {poll_id} not found in active polls")
                return

            view = self.active_polls[poll_id]
            votes = view.get_votes()
            
            if not votes:
                await self.main_channel.send("No votes were cast!")
                return

            # Count votes
            vote_counts = {}
            for voter_id, target_id in votes.items():
                vote_counts[target_id] = vote_counts.get(target_id, 0) + 1

            # Find player(s) with most votes
            max_votes = max(vote_counts.values())
            potential_targets = [pid for pid, count in vote_counts.items() if count == max_votes]
            
            # Randomly choose from tied players
            eliminated_id = random.choice(potential_targets)
            eliminated_player = self.players[eliminated_id]
            
            # Remove player from alive list
            if eliminated_id in self.alive_players:
                self.alive_players.remove(eliminated_id)
                self.dead_players.append(eliminated_id)
                await self.main_channel.send(
                    f"The town has spoken! **{eliminated_player.name}** has been eliminated. "
                    f"They were a **{self.player_roles[eliminated_id].value}**."
                )
            
            # Clear the poll
            del self.active_polls[poll_id]
            
            # Check win conditions
            await self.check_win_conditions()
            
        except Exception as e:
            print(f"ERROR in process_votes: {e}")

    async def process_night_actions(self):
        """Process all night actions and determine outcomes"""
        try:
            if not self.night_actions:
                print("DEBUG - No night actions recorded")
                await self.main_channel.send("No actions were taken during the night.")
                return

            print("\nDEBUG - Processing night actions:")
            print(f"Current night actions: {self.night_actions}")

            # Process detective's investigation first
            detective_actions = []
            for pid, action_data in self.night_actions.items():
                if action_data['action'] == 'investigate':
                    detective_actions.append((pid, action_data['target']))
                    
            if detective_actions:
                _, target_id = detective_actions[0]
                detective_id = detective_actions[0][0]
                target_role = self.player_roles[target_id]
                target_player = self.players[target_id]
                print(f"DEBUG - Detective {self.players[detective_id].name} investigated {target_player.name}")
                if self.detective_channel:
                    await self.detective_channel.send(f"🔍 Investigation results: **{target_player.name}** is a **{target_role.value}**!")

            # Process doctor's save
            doctor_actions = []
            for pid, action_data in self.night_actions.items():
                if action_data['action'] == 'save':
                    doctor_actions.append((pid, action_data['target']))
                    
            saved_id = doctor_actions[0][1] if doctor_actions else None
            if doctor_actions:
                doctor_id = doctor_actions[0][0]
                print(f"DEBUG - Doctor {self.players[doctor_id].name} protected {self.players[saved_id].name}")
            else:
                print("DEBUG - No doctor action")

            # Process mafia's kill last
            mafia_actions = []
            for pid, action_data in self.night_actions.items():
                if action_data['action'] == 'kill':
                    mafia_actions.append((pid, action_data['target']))

            if mafia_actions:
                print("\nDEBUG - Mafia votes:")
                vote_counts = {}
                for pid, target_id in mafia_actions:
                    vote_counts[target_id] = vote_counts.get(target_id, 0) + 1
                    print(f"Mafia member {self.players[pid].name} voted to kill {self.players[target_id].name}")
                
                print(f"\nDEBUG - Vote counts:")
                for target_id, count in vote_counts.items():
                    print(f"{self.players[target_id].name}: {count} votes")
                
                max_votes = max(vote_counts.values())
                potential_targets = [pid for pid, count in vote_counts.items() if count == max_votes]
                target_id = random.choice(potential_targets)
                
                target_player = self.players[target_id]
                print(f"\nDEBUG - Final target selected: {target_player.name}")
                print(f"DEBUG - Doctor saved ID: {saved_id}")
                print(f"DEBUG - Target ID: {target_id}")
                
                if str(target_id) == str(saved_id):  # Convert both to strings for comparison
                    print("DEBUG - Target was saved by doctor!")
                    await self.main_channel.send(f"🏥 The Doctor successfully saved someone from death!")
                else:
                    if target_id in self.alive_players:
                        self.alive_players.remove(target_id)
                        self.dead_players.append(target_id)
                        print(f"DEBUG - {target_player.name} was killed")
                        await self.main_channel.send(f"💀 **{target_player.name}** was found dead! They were a **{self.player_roles[target_id].value}**.")
                    else:
                        print(f"ERROR - Target {target_id} not in alive_players")

            # Clear night actions
            self.night_actions.clear()
            
            # Check win conditions
            await self.check_win_conditions()
            
        except Exception as e:
            print(f"ERROR in process_night_actions: {e}")
            print(f"Night actions at time of error: {self.night_actions}")

    async def start_day_phase(self):
        """Start the day phase of the game"""
        self.state = GameState.DAY
        
        # Generate day transition story
        day_prompt = "Create a scene describing the village coming to life as dawn breaks, with tension in the air as villagers gather to discuss the night's events."
        day_story = await self.storyteller.generate_story(day_prompt)
        await self.main_channel.send(day_story)
        
        # Move to voting phase after a short discussion period
        await asyncio.sleep(30)
        await self.start_voting_phase()

    async def night_phase(self):
        """Start the night phase of the game"""
        self.state = GameState.NIGHT
        self.night_actions.clear()
        
        # Notify all players that night has begun
        await self.main_channel.send("🌙 Night falls on the village. All players check your role channels for actions. You have 45 seconds!")
        
        # Count how many actions we expect
        expected_actions = 0
        
        # Send prompts to special roles with dropdown menus
        mafia_members = [self.players[pid] for pid, role in self.player_roles.items() 
                        if role == Role.MAFIA and pid in self.alive_players]
        if mafia_members:
            expected_actions += 1
            alive_targets = [pid for pid in self.alive_players 
                           if pid not in [m.id for m in mafia_members]]
            if alive_targets:
                view = KillView(self, alive_targets)
                await self.mafia_channel.send("🔪 Choose your target to kill:", view=view)
        
        detective_members = [self.players[pid] for pid, role in self.player_roles.items() 
                           if role == Role.DETECTIVE and pid in self.alive_players]
        if detective_members:
            expected_actions += 1
            alive_targets = [pid for pid in self.alive_players 
                           if pid != detective_members[0].id]
            if alive_targets:
                view = InvestigateView(self, alive_targets)
                await self.detective_channel.send("🔍 Choose a player to investigate:", view=view)
        
        doctor_members = [self.players[pid] for pid, role in self.player_roles.items() 
                         if role == Role.DOCTOR and pid in self.alive_players]
        if doctor_members:
            expected_actions += 1
            view = ProtectView(self, self.alive_players)
            await self.doctor_channel.send("💉 Choose a player to protect:", view=view)

        # Wait for either all actions to be submitted or timeout
        try:
            while len(self.night_actions) < expected_actions:
                if len(self.night_actions) >= expected_actions:
                    break
                await asyncio.sleep(1)
                
        except asyncio.TimeoutError:
            pass
        
        # Process night actions and transition to day
        await self.end_night()

    async def end_night(self):
        """Process all night actions and transition to day phase"""
        # Process night actions in order: Doctor -> Detective -> Mafia
        protected_player = None
        killed_player = None
        
        # Process doctor's protection
        for player_id, action in self.night_actions.items():
            if self.player_roles[player_id] == Role.DOCTOR:
                protected_player = action['target']
                break
        
        # Process mafia's kill
        for player_id, action in self.night_actions.items():
            if self.player_roles[player_id] == Role.MAFIA:
                if action['target'] != protected_player:
                    killed_player = action['target']
                break
        
        # Send night results
        await self.main_channel.send("☀️ The sun rises on the village...")
        
        if killed_player:
            player = self.players[killed_player]
            self.alive_players.remove(killed_player)
            self.dead_players.append(killed_player)
            await self.main_channel.send(f"😱 {player.display_name} was killed during the night!")
        else:
            await self.main_channel.send("😌 Nobody died during the night.")
        
        # Process detective's investigation (only send to detective)
        for player_id, action in self.night_actions.items():
            if self.player_roles[player_id] == Role.DETECTIVE:
                target_id = action['target']
                target_role = self.player_roles[target_id]
                detective = self.players[player_id]
                await self.detective_channel.send(
                    f"🔍 Your investigation reveals that {self.players[target_id].display_name} "
                    f"is a {target_role.name}!"
                )
                break
        
        # Transition to day phase
        self.state = GameState.DAY
        # Start day phase logic here...

    async def start_voting_phase(self):
        """Start the voting phase during the day"""
        try:
            # Get list of alive players
            alive_players = [self.players[pid] for pid in self.alive_players]
            
            # Create the voting poll
            poll_msg = await self.create_action_poll(
                channel=self.main_channel,
                action_type="eliminate",
                alive_players=alive_players,
                timeout=30
            )
            
            if not poll_msg:
                print("ERROR: Failed to create voting poll")
                return
                
            # Wait for votes
            await asyncio.sleep(30)
            
            # Process votes
            await self.process_votes(poll_msg.id)
            
        except Exception as e:
            print(f"ERROR in start_voting_phase: {e}")

    async def eliminate_player(self, player_id: int):
        """Eliminate a player from the game"""
        if player_id in self.alive_players:
            self.alive_players.remove(player_id)
            player = self.players[player_id]
            role = self.player_roles.get(player_id, "Villager")
            
            # Announce elimination
            await self.main_channel.send(
                f"🪦 The village has decided to eliminate **{player.display_name}**.\n"
                f"They were a **{role}**!"
            )
            
            # Check win conditions
            await self.check_win_condition()
            
    def get_player_status_message(self):
        """Get a formatted message showing alive and dead players"""
        alive_players = [self.players[pid].name for pid in self.alive_players]
        dead_players = [self.players[pid].name for pid in self.players.keys() if pid not in self.alive_players]
        
        msg = "**Player Status**\n"
        msg += "🟢 **Alive**: " + ", ".join(alive_players) + "\n"
        if dead_players:
            msg += "💀 **Dead**: " + ", ".join(dead_players)
        return msg

    async def check_win_conditions(self) -> bool:
        """Check if either faction has won"""
        mafia_count = sum(1 for pid in self.alive_players if self.player_roles[pid] == Role.MAFIA)
        villager_count = len(self.alive_players) - mafia_count
        
        if mafia_count == 0:
            # Village wins
            prompt = "Create an triumphant ending where the village successfully eliminated all mafia members and peace is restored."
            story = await self.storyteller.generate_story(prompt)
            await self.main_channel.send(f"{story}\n\n The Village has won! ")
            return True
        elif mafia_count >= villager_count:
            # Mafia wins
            prompt = "Create a dark ending where the mafia has gained control of the village, striking fear into the hearts of the remaining villagers."
            story = await self.storyteller.generate_story(prompt)
            await self.main_channel.send(f"{story}\n\n The Mafia has won! ")
            return True
        return False

    async def reset_game(self):
        """Reset the game state and clean up resources"""
        # Clean up mafia chat channel if it exists
        if self.mafia_channel:
            try:
                await self.mafia_channel.delete()
            except:
                pass
            self.mafia_channel = None
        
        # Reset all game state
        self.players.clear()
        self.player_roles.clear()
        self.dead_players = set()
        self.night_actions = {}
        self.votes = {}
        self.state = GameState.WAITING
        self.main_channel = None
        self.mafia_channel = None
        self.detective_channel = None
        self.doctor_channel = None
        self.villager_channel = None
        self.npc_base_id = 1000000

    async def remove_player(self, player_id: int) -> bool:
        """Remove a player from the game and handle necessary adjustments"""
        if player_id not in self.players:
            return False
            
        player = self.players[player_id]
        
        # Remove from all game states
        self.players.pop(player_id)
        self.player_roles.pop(player_id, None)
        if player_id in self.alive_players:
            self.alive_players.remove(player_id)
        if player_id in self.dead_players:
            self.dead_players.remove(player_id)
        self.current_votes = {k: v for k, v in self.current_votes.items() if k != player_id and v != player_id}
        self.night_actions = {k: v for k, v in self.night_actions.items() if k != player_id and v != player_id}
        
        # Remove from mafia chat if applicable
        if self.mafia_channel and self.player_roles.get(player_id) == Role.MAFIA:
            try:
                await self.mafia_channel.set_permissions(player, overwrite=None)
            except:
                pass
        
        # Generate quit story
        if self.state != GameState.WAITING:
            prompt = f"Create a dramatic description of {player.name}'s sudden and mysterious departure from the village."
            story = await self.storyteller.generate_story(prompt)
            if self.main_channel:
                await self.main_channel.send(story)
        
        # Check if game should end due to player count
        if self.state != GameState.WAITING and len(self.players) < 4:
            await self.main_channel.send("Not enough players remaining. The game must end.")
            await self.reset_game()
            return True
            
        # Check win conditions if game is in progress
        if self.state != GameState.WAITING:
            await self.check_win_conditions()
            
        return True

    async def get_npc_action(self, npc_id: int, action_type: str) -> Optional[int]:
        """Get an AI-generated action for an NPC"""
        if not self.storyteller:
            return random.choice([pid for pid in self.alive_players if pid != npc_id])
            
        npc = self.players[npc_id]
        npc_role = self.player_roles[npc_id]
        
        # Get list of valid targets
        targets = [pid for pid in self.alive_players if pid != npc_id]
        if not targets:
            return None
            
        target_names = [self.players[pid].name for pid in targets]
        
        # Create context for the AI
        context = f"You are {npc.name}, a {npc_role.value} in a game of Mafia. "
        
        if action_type == "vote":
            context += f"You must vote to eliminate one player who you suspect is in the mafia. The candidates are: {', '.join(target_names)}."
        elif action_type == "mafia_kill":
            context += f"As a mafia member, you must choose a villager to eliminate. The potential targets are: {', '.join(target_names)}."
        elif action_type == "investigate":
            context += f"As the detective, you must choose a player to investigate. The suspects are: {', '.join(target_names)}."
        elif action_type == "protect":
            context += f"As the doctor, you must choose a player to protect from the mafia. The potential targets are: {', '.join(target_names)}."
            
        try:
            # Create a mock discord message
            mock_message = type('MockMessage', (), {'content': context})()
            chosen_name = await self.storyteller.agent.run(mock_message)
            chosen_name = chosen_name.strip()
            
            # Find the closest matching player name
            best_match = None
            for pid in targets:
                if self.players[pid].name.lower() in chosen_name.lower():
                    best_match = pid
                    break
                    
            if best_match is not None:
                return best_match
            
            # Fallback to random choice if no match found
            return random.choice(targets) if targets else None
            
        except Exception as e:
            print(f"Error getting NPC action: {e}")
            # Return a random target as fallback
            return random.choice(targets) if targets else None

    async def process_npc_actions(self):
        """Process actions for all NPCs during appropriate game phases"""
        if self.state == GameState.NIGHT:
            for player_id, role in self.player_roles.items():
                if player_id >= self.npc_base_id and player_id in self.alive_players:
                    if role == Role.MAFIA:
                        target = await self.get_npc_action(player_id, "mafia_kill")
                        if target:
                            self.night_actions[player_id] = target
                    elif role == Role.DETECTIVE:
                        target = await self.get_npc_action(player_id, "investigate")
                        if target:
                            self.night_actions[player_id] = target
                    elif role == Role.DOCTOR:
                        target = await self.get_npc_action(player_id, "protect")
                        if target:
                            self.night_actions[player_id] = target
        
        elif self.state == GameState.VOTING:
            for player_id in self.alive_players:
                if player_id >= self.npc_base_id and player_id not in self.current_votes:
                    target = await self.get_npc_action(player_id, "vote")
                    if target:
                        self.current_votes[player_id] = target
                        
                        # Generate voting story for NPC
                        npc = self.players[player_id]
                        target_player = self.players[target]
                        prompt = f"Create a dramatic moment where {npc.name} accuses {target_player.name} of being in league with the mafia."
                        story = await self.storyteller.generate_story(prompt)
                        await self.main_channel.send(story)

    async def setup_channels(self):
        """Set up game channels"""
        try:
            # Create category for game channels
            category = await self.guild.create_category("Mafia Game")
            
            # Create role-specific channels
            self.mafia_channel = await self.guild.create_text_channel(
                'mafia-chat',
                category=category
            )
            
            self.detective_channel = await self.guild.create_text_channel(
                'detective-chat',
                category=category
            )
            
            self.doctor_channel = await self.guild.create_text_channel(
                'doctor-chat',
                category=category
            )
            
            # Set default permissions (deny access to everyone)
            for channel in [self.mafia_channel, self.detective_channel, self.doctor_channel]:
                await channel.set_permissions(self.guild.default_role, read_messages=False, send_messages=False)
            
            print("DEBUG - Channels created successfully")
            
        except Exception as e:
            print(f"ERROR in setup_channels: {e}")

    async def assign_channel_permissions(self):
        """Assign channel permissions based on roles"""
        try:
            for player_id, role in self.player_roles.items():
                player = self.players[player_id]
                
                if role == Role.MAFIA:
                    await self.mafia_channel.set_permissions(player, read_messages=True, send_messages=True)
                    print(f"DEBUG - Gave mafia access to {player.name}")
                elif role == Role.DETECTIVE:
                    await self.detective_channel.set_permissions(player, read_messages=True, send_messages=True)
                    print(f"DEBUG - Gave detective access to {player.name}")
                elif role == Role.DOCTOR:
                    await self.doctor_channel.set_permissions(player, read_messages=True, send_messages=True)
                    print(f"DEBUG - Gave doctor access to {player.name}")
                    
            print("DEBUG - Channel permissions assigned successfully")
            
        except Exception as e:
            print(f"ERROR in assign_channel_permissions: {e}")

    async def cleanup_channels(self):
        """Clean up game channels"""
        try:
            channels_to_delete = [
                self.mafia_channel,
                self.detective_channel,
                self.doctor_channel
            ]
            
            for channel in channels_to_delete:
                if channel:
                    await channel.delete()
            
            # Also delete the category if it exists
            if self.mafia_channel and self.mafia_channel.category:
                await self.mafia_channel.category.delete()
                
            print("DEBUG - Channels cleaned up successfully")
            
        except Exception as e:
            print(f"ERROR in cleanup_channels: {e}")

    def reset_game_state(self):
        """Reset all game state variables"""
        # Reset all game state
        self.players.clear()
        self.player_roles.clear()
        self.alive_players.clear()
        self.dead_players.clear()
        self.night_actions.clear()
        self.active_polls.clear()
        self.current_votes.clear()
        self.game_started = False
        self.state = GameState.WAITING

    async def day_phase(self):
        """Start the day phase of the game"""
        self.state = GameState.DAY
        
        # Generate day story
        day_story = await self.storyteller.generate_story(self.story_prompts['day'])
        
        # Get status update
        status_msg = self.get_player_status_message()
        
        # Send day phase message
        await self.main_channel.send(
            f"☀️ **Day Phase Begins** ☀️\n\n"
            f"{day_story}\n\n"
            f"The village awakens to discuss the night's events...\n\n"
            f"{status_msg}"
        )
        
        # Start voting phase after discussion period
        await self.main_channel.send("The village will have 60 seconds for discussion before voting begins...")
        await asyncio.sleep(60)  # 60 seconds for discussion
        
        if self.state == GameState.DAY:  # Only proceed if still in day phase
            await self.start_voting_phase()

    async def create_role_channels(self):
        """Create private channels for special roles"""
        # Create category for mafia game channels if it doesn't exist
        category = await self.guild.create_category("Mafia Game", overwrites={
            self.guild.default_role: discord.PermissionOverwrite(read_messages=False)
        })
        
        # Create mafia channel
        self.mafia_channel = await self.guild.create_text_channel('mafia-chat', category=category)
        # Create detective channel
        self.detective_channel = await self.guild.create_text_channel('detective-chat', category=category)
        # Create doctor channel
        self.doctor_channel = await self.guild.create_text_channel('doctor-chat', category=category)
        
        # Set permissions for each channel
        for player_id, role in self.player_roles.items():
            player = self.guild.get_member(player_id)
            if role == Role.MAFIA:
                await self.mafia_channel.set_permissions(player, read_messages=True, send_messages=True)
            elif role == Role.DETECTIVE:
                await self.detective_channel.set_permissions(player, read_messages=True, send_messages=True)
            elif role == Role.DOCTOR:
                await self.doctor_channel.set_permissions(player, read_messages=True, send_messages=True)

    async def start_night(self):
        """Start the night phase of the game"""
        self.state = GameState.NIGHT
        self.night_actions.clear()
        
        # Notify all players that night has begun
        await self.main_channel.send("🌙 Night falls on the village. All players check your role channels for actions. You have 45 seconds!")
        
        # Count how many actions we expect
        expected_actions = 0
        
        # Send prompts to special roles with dropdown menus
        mafia_members = [self.players[pid] for pid, role in self.player_roles.items() 
                        if role == Role.MAFIA and pid in self.alive_players]
        if mafia_members:
            expected_actions += 1
            alive_targets = [pid for pid in self.alive_players 
                           if pid not in [m.id for m in mafia_members]]
            if alive_targets:
                view = KillView(self, alive_targets)
                await self.mafia_channel.send("🔪 Choose your target to kill:", view=view)
        
        detective_members = [self.players[pid] for pid, role in self.player_roles.items() 
                           if role == Role.DETECTIVE and pid in self.alive_players]
        if detective_members:
            expected_actions += 1
            alive_targets = [pid for pid in self.alive_players 
                           if pid != detective_members[0].id]
            if alive_targets:
                view = InvestigateView(self, alive_targets)
                await self.detective_channel.send("🔍 Choose a player to investigate:", view=view)
        
        doctor_members = [self.players[pid] for pid, role in self.player_roles.items() 
                         if role == Role.DOCTOR and pid in self.alive_players]
        if doctor_members:
            expected_actions += 1
            view = ProtectView(self, self.alive_players)
            await self.doctor_channel.send("💉 Choose a player to protect:", view=view)

        # Wait for either all actions to be submitted or timeout
        try:
            while len(self.night_actions) < expected_actions:
                if len(self.night_actions) >= expected_actions:
                    break
                await asyncio.sleep(1)
                
        except asyncio.TimeoutError:
            pass
        
        # Process night actions and transition to day
        await self.end_night()

    async def end_night(self):
        """Process all night actions and transition to day phase"""
        # Process night actions in order: Doctor -> Detective -> Mafia
        protected_player = None
        killed_player = None
        
        # Process doctor's protection
        for player_id, action in self.night_actions.items():
            if self.player_roles[player_id] == Role.DOCTOR:
                protected_player = action['target']
                break
        
        # Process mafia's kill
        for player_id, action in self.night_actions.items():
            if self.player_roles[player_id] == Role.MAFIA:
                if action['target'] != protected_player:
                    killed_player = action['target']
                break
        
        # Send night results
        await self.main_channel.send("☀️ The sun rises on the village...")
        
        if killed_player:
            player = self.players[killed_player]
            self.alive_players.remove(killed_player)
            self.dead_players.append(killed_player)
            await self.main_channel.send(f"😱 {player.display_name} was killed during the night!")
        else:
            await self.main_channel.send("😌 Nobody died during the night.")
        
        # Process detective's investigation (only send to detective)
        for player_id, action in self.night_actions.items():
            if self.player_roles[player_id] == Role.DETECTIVE:
                target_id = action['target']
                target_role = self.player_roles[target_id]
                detective = self.players[player_id]
                await self.detective_channel.send(
                    f"🔍 Your investigation reveals that {self.players[target_id].display_name} "
                    f"is a {target_role.name}!"
                )
                break
        
        # Transition to day phase
        self.state = GameState.DAY
        # Start day phase logic here...

    async def handle_kill_command(self, ctx, target_name):
        """Handle the kill command from mafia members"""
        if self.state != GameState.NIGHT:
            return await ctx.send("You can only kill during the night phase!")
        
        if ctx.author.id not in self.alive_players:
            return await ctx.send("Dead players cannot perform actions!")
            
        if self.player_roles[ctx.author.id] != Role.MAFIA:
            return await ctx.send("Only mafia members can use this command!")
            
        if ctx.channel != self.mafia_channel:
            return await ctx.send("This command can only be used in the mafia channel!")

        # Find target player
        target_id = None
        for pid in self.alive_players:
            if self.players[pid].display_name.lower() == target_name.lower():
                target_id = pid
                break
                
        if target_id is None:
            return await ctx.send(f"Could not find player: {target_name}")
            
        # Don't allow mafia to kill themselves
        if target_id == ctx.author.id:
            return await ctx.send("You cannot target yourself!")

        self.night_actions[ctx.author.id] = {
            'action': 'kill',
            'target': target_id
        }
        await ctx.send(f"You have chosen to kill {target_name}")

    async def handle_protect_command(self, ctx, target_name):
        """Handle the protect command from the doctor"""
        if self.state != GameState.NIGHT:
            return await ctx.send("You can only protect during the night phase!")
        
        if ctx.author.id not in self.alive_players:
            return await ctx.send("Dead players cannot perform actions!")
            
        if self.player_roles[ctx.author.id] != Role.DOCTOR:
            return await ctx.send("Only the doctor can use this command!")
            
        if ctx.channel != self.doctor_channel:
            return await ctx.send("This command can only be used in the doctor channel!")

        # Find target player
        target_id = None
        for pid in self.alive_players:
            if self.players[pid].display_name.lower() == target_name.lower():
                target_id = pid
                break
                
        if target_id is None:
            return await ctx.send(f"Could not find player: {target_name}")

        self.night_actions[ctx.author.id] = {
            'action': 'protect',
            'target': target_id
        }
        await ctx.send(f"You have chosen to protect {target_name}")

    async def handle_investigate_command(self, ctx, target_name):
        """Handle the investigate command from the detective"""
        if self.state != GameState.NIGHT:
            return await ctx.send("You can only investigate during the night phase!")
        
        if ctx.author.id not in self.alive_players:
            return await ctx.send("Dead players cannot perform actions!")
            
        if self.player_roles[ctx.author.id] != Role.DETECTIVE:
            return await ctx.send("Only the detective can use this command!")
            
        if ctx.channel != self.detective_channel:
            return await ctx.send("This command can only be used in the detective channel!")

        # Find target player
        target_id = None
        for pid in self.alive_players:
            if self.players[pid].display_name.lower() == target_name.lower():
                target_id = pid
                break
                
        if target_id is None:
            return await ctx.send(f"Could not find player: {target_name}")
            
        # Don't allow detective to investigate themselves
        if target_id == ctx.author.id:
            return await ctx.send("You cannot investigate yourself!")

        self.night_actions[ctx.author.id] = {
            'action': 'investigate',
            'target': target_id
        }
        await ctx.send(f"You have chosen to investigate {target_name}")

@bot.command(name='vote')
async def vote(ctx):
    """Legacy vote command - now redirects to the poll system"""
    if ctx.guild is None:
        return
        
    game = games.get(ctx.guild.id)
    if not game or game.state != GameState.VOTING:
        return
        
    await ctx.send("Please use the poll above to cast your vote! ⬆️")

@bot.command(name='kill')
async def kill(ctx, *, target_name):
    """Command for mafia to kill a player"""
    game = games.get(ctx.guild.id)
    if game:
        await game.handle_kill_command(ctx, target_name)

@bot.command(name='protect')
async def protect(ctx, *, target_name):
    """Command for doctor to protect a player"""
    game = games.get(ctx.guild.id)
    if game:
        await game.handle_protect_command(ctx, target_name)

@bot.command(name='investigate')
async def investigate(ctx, *, target_name):
    """Command for detective to investigate a player"""
    game = games.get(ctx.guild.id)
    if game:
        await game.handle_investigate_command(ctx, target_name)

@bot.command(name='endgame')
async def end_game(ctx):
    """End the current game"""
    global current_game
    
    if current_game is None:
        await ctx.send("No game is currently running!")
        return
        
    # Clean up channels first
    await current_game.cleanup_channels()
    
    # Reset game state
    current_game.reset_game_state()
    
    await ctx.send("Game ended. All channels have been cleaned up.")

# Global game instance
current_game = None

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')

@bot.command(name='startgame')
async def start_game(ctx):
    """Start a new game of Mafia"""
    global current_game
    
    if current_game is not None and current_game.state != GameState.WAITING:
        await ctx.send("A game is already in progress!")
        return
        
    current_game = MafiaGame(ctx.guild, ctx.channel)
    await ctx.send("Starting a new game of Mafia! Type !join to join the game.")

@bot.command(name='join')
async def join_game(ctx):
    """Join the current game"""
    global current_game
    
    if current_game is None:
        await ctx.send("No game is currently running! Use !startgame to start a new game.")
        return
        
    if current_game.state != GameState.WAITING:
        await ctx.send("Cannot join a game that has already started!")
        return
        
    if ctx.author.id in current_game.players:
        await ctx.send("You have already joined the game!")
        return
        
    current_game.players[ctx.author.id] = ctx.author
    await ctx.send(f"{ctx.author.name} has joined the game!")

@bot.command(name='begin')
async def begin_game(ctx):
    """Begin the game with current players"""
    global current_game
    
    if current_game is None:
        await ctx.send("No game is currently running! Use !startgame to start a new game.")
        return
        
    await current_game.begin_game()

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
